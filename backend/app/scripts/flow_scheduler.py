# -*- coding: utf-8 -*-
"""
@File    : flow_scheduler.py
@Time    : 2026/6/10 17:29
@Author  : tanlei
"""
import time
import re

"""DAG流程调度器：支持优先级、超时、3次重试、状态上报、多流程隔离"""
import os
import json
import uuid
import random
from datetime import datetime
from typing import Dict, Any, List, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed
from app.core.vector_db import ChromaDBSingleton, page_matches
from app.core.config import config
from app.utils.case_condenser import cases_from_markdown, condense_cases, next_level
from app.utils.context_manager import context_manager
from app.utils.file_output_manager import output_manager
from app.scripts.skill_state_manager import skill_state_manager
import logging

logger = logging.getLogger("FLOW_SCHEDULER")

# 评审线的输入预算（字数上限）。中文场景按 1 字 ≈ 1 token 保守估算，
# 预留输出（LLM_MAX_TOKENS=8192）与安全余量后取 8 万字；可用环境变量 MAX_REVIEW_INPUT_CHARS 覆盖。
# 参考量级：通用规则 1.3k + 池页规则 11.8k + 精简用例(L1) 6.8k + 文档 50k ≈ 70k 字，仍在预算内；
#          模块层评审（规则=全页 41k）+ 长文档时会触发裁剪，裁剪明细会回传前端。
DEFAULT_MAX_REVIEW_INPUT_CHARS = 80000

# 文档被裁剪时追加的提示语（长度需在计算 allow 时预留，否则会溢出预算误触发下一档裁剪）
_DOC_TRUNC_NOTICE = "\n\n【注意】文档过长，超出评审输入预算部分已截断，如需完整评审请拆分文档或分次提交。"


def compose_requirement(requirement: str, doc_text: str = "") -> str:
    """
    统一「上传文档段 + 需求描述段」的合成规则（全项目唯一实现）。

    历史教训：这段拼接曾在 /review、/generate-direct、ReqReview.run 三处各写一份，
    导致「评审看得到文档、第二步生成看不到文档」这类不一致。现统一走本函数，
    保证评审线与生成线拿到的是**同一份文本**。
    """
    req = (requirement or "").strip()
    doc = (doc_text or "").strip()
    if not doc:
        return req
    return f"【用户上传的参考文档内容】\n{doc}\n\n【用户需求描述】\n{req}"


class FlowScheduler:
    # 增量归并输入的内存缓存上限（供「删除确认」二次归并复用；落盘副本用于重启后降级）
    _MERGE_CACHE_MAX = 20

    def __init__(self):
        self.vector_db = ChromaDBSingleton.get_instance()
        self._skill_instances = {}
        # 增量归并输入缓存：{slug: {history_cases, incremental_cases, module_rules, ...}}
        # 用途：hold 模式生成的产物保留全部用例，人工勾选删除后需要「用同一份输入」二次归并，
        #       缓存它可避免重新召回基线、重新调生成模型。
        self._merge_inputs: Dict[str, Dict[str, Any]] = {}
        self._merge_inputs_order: List[str] = []

    def _get_skill(self, skill_name: str):
        """懒加载Skill实例，避免循环导入"""
        if skill_name in self._skill_instances:
            return self._skill_instances[skill_name]

        if skill_name == "req-review":
            from app.scripts.req_review import ReqReview
            inst = ReqReview()
        elif skill_name == "req-review-associator":
            from app.scripts.req_review_associator import ReqReviewAssociator
            inst = ReqReviewAssociator()
        elif skill_name == "req-review-challenger":
            from app.scripts.req_review_challenger import ReqReviewChallenger
            inst = ReqReviewChallenger()
        elif skill_name == "req-review-main":
            from app.scripts.req_review_main import ReqReviewMain
            inst = ReqReviewMain()
        elif skill_name == "req-test-cases":
            from app.scripts.req_test_cases import ReqTestCases
            inst = ReqTestCases()
        elif skill_name == "case-diff-analyzer":
            from app.scripts.case_diff_analyzer import CaseDiffAnalyzer
            inst = CaseDiffAnalyzer()
        elif skill_name == "rule-patch-proposer":
            from app.scripts.rule_patch_proposer import RulePatchProposer
            inst = RulePatchProposer()
        elif skill_name == "case-quality-scorer":
            from app.scripts.case_quality_scorer import CaseQualityScorer
            inst = CaseQualityScorer()
        else:
            logger.error(f"❌ 未知Skill: {skill_name}")
            raise Exception(f"未知Skill: {skill_name}")

        self._skill_instances[skill_name] = inst
        logger.debug(f"🔧 实例化Skill | skill={skill_name} | 优先级={inst.priority} | 超时={inst.timeout}s")
        return inst

    def _retrieve_full_context(
            self,
            flow_id: str,
            query: str,
            skill_filter: str = None,
            biz_module: str = None,
            page: str = None
    ) -> Dict[str, str]:
        """
        结构化上下文检索（按来源分类返回，避免规则/历史/示例混作一团，模型无法区分）

        返回结构：
        {
            "business_docs": 用户上传的业务文档,
            "common_rules":  通用交互规则（强制注入，不依赖语义召回）,
            "module_rules":  模块专属规则（按 biz_module + page 精确取全文）,
            "skill_examples": 技能主文件（输出格式规范与样例）,
            "history_cases": 历史沉淀资产,
        }

        设计要点：
        1. 通用规则（rule_scope=common）与需求描述语义距离通常较远，
           走 top-K 语义召回极易漏召，因此单独强制注入，保证通用校验项不缺失。
        2. 模块专属规则同理改为**精确取全文**（get_module_rules_full）：
           语义 top-K 不保证命中规则文件，漏召会让 module_rules 为空 →
           ReqTestCases 提取 0 个 R/S 编号 → 静默退化为单次全量生成（无分片/无编号自检）。
           仅在未指定 biz_module 时，才回退到原有的语义召回行为。
        3. 页面级隔离（page）：模块层生成（page为空）召回该模块全部页面；
           页面层生成（page非空）只召回模块层 + 本页的规则与历史用例，排除其他页。
        """
        logger.info(
            f"🔍 开始上下文检索 | flow_id={flow_id} | skill_filter={skill_filter} | "
            f"biz_module={biz_module or '未指定'} | page={page or '模块层'} | query长度={len(query)}字"
        )

        res = self.vector_db.search_all(query, n_results=5, biz_module=biz_module, page=page)

        business_docs = ""
        module_rules = ""
        skill_examples = ""
        history_cases = ""

        # 模块专属规则：指定 biz_module 时按 (biz_module, page) 精确取全文，不依赖语义召回
        precise_rules = False
        if biz_module:
            module_rules = self.vector_db.get_module_rules_full(
                biz_module, page=page, skill_name=skill_filter
            )
            precise_rules = bool(module_rules.strip())

        for doc, meta in zip(res["documents"][0], res["metadatas"][0]):
            if meta["type"] == "business":
                business_docs += doc + "\n"
            elif meta["type"] == "skill":
                if skill_filter is None or meta.get("skill_name") == skill_filter:
                    # 主SKILL.md 承载输出格式规范；references 承载模块专属规则
                    if meta.get("file_type") == "main":
                        skill_examples += doc + "\n"
                    elif not precise_rules:
                        # 仅在未走精确取全文时，才用语义召回结果兜底
                        # 页面级隔离：references 规则需命中目标页才注入
                        if page_matches(meta.get("page"), page):
                            module_rules += doc + "\n"
            elif meta["type"] == "project_asset":
                # 页面级隔离：历史沉淀资产需命中目标页才召回
                if page_matches(meta.get("page"), page):
                    history_cases += f"\n【历史相似{meta['asset_type']}】：\n{doc}\n"

        # 通用规则强制注入：不依赖语义召回，避免通用校验项漏召
        common_rules = self.vector_db.get_common_rules(skill_name=skill_filter)

        ctx = {
            "business_docs": business_docs,
            "common_rules": common_rules,
            "module_rules": module_rules,
            "skill_examples": skill_examples,
            "history_cases": history_cases,
        }

        logger.info(
            f"✅ 上下文检索完成 | flow_id={flow_id} | "
            f"业务文档={len(business_docs)}字 | 通用规则={len(common_rules)}字 | "
            f"模块规则={len(module_rules)}字 | 技能示例={len(skill_examples)}字 | "
            f"历史沉淀={len(history_cases)}字"
        )
        if not common_rules:
            logger.warning(
                f"⚠️ 未检索到通用规则，页面类用例可能缺失通用交互校验项 | skill_filter={skill_filter}"
            )
        if biz_module and not module_rules.strip():
            logger.warning(
                f"⚠️ 未取到模块专属规则，用例将不做编号分片/编号自检（静默降级） | "
                f"biz_module={biz_module} | page={page or '模块层'} | skill={skill_filter} | "
                f"请检查 references 下规则文件的 front matter（biz_module / page / rule_scope）"
            )

        # 兼容留存：整体上下文仍写入 rag_context
        context_manager.set_rag_context(
            flow_id,
            business_docs,
            f"{common_rules}\n{module_rules}\n{skill_examples}"
        )
        return ctx

    def run_parallel_skills(self, flow_id: str, skill_names: List[str], input_ctx: Dict[str, Any]) -> Dict[str, Any]:
        """并行执行Skill：按优先级排序 + 状态上报"""
        logger.info(f"🚀 并行执行Skill组: {skill_names} | 流程ID:{flow_id}")
        skill_list = [(self._get_skill(name), name) for name in skill_names]
        skill_list.sort(key=lambda x: x[0].priority)

        skill_state_manager.init_flow(flow_id, skill_names)

        results = {}
        with ThreadPoolExecutor(max_workers=len(skill_names)) as executor:
            future_map = {}
            for skill_inst, name in skill_list:
                skill_inst.set_flow_id(flow_id)
                skill_state_manager.update_skill_start(flow_id, name)
                future = executor.submit(skill_inst.run, input_ctx)
                future_map[future] = name

            for future in as_completed(future_map):
                name = future_map[future]
                try:
                    res = future.result()
                    results[name] = res
                    skill_state_manager.update_skill_success(flow_id, name, str(res)[:200])
                    logger.info(f"✅ 并行Skill[{name}] 执行成功 | flow_id={flow_id}")
                except Exception as e:
                    logger.error(f"❌ 并行Skill[{name}] 执行失败 | flow_id={flow_id} | 错误: {str(e)}")
                    skill_state_manager.update_skill_failed(flow_id, name, str(e))
                    raise
        return results

    def run_skill_chain(self, flow_id: str, skill_chain: List[str], input_ctx: Dict[str, Any]) -> Dict[str, Any]:
        """串行执行Skill链"""
        logger.info(f"🔗 串行执行Skill链: {skill_chain} | 流程ID:{flow_id}")
        skill_state_manager.init_flow(flow_id, skill_chain)
        current_ctx = input_ctx.copy()

        for name in skill_chain:
            logger.info(f"▶️ 串行执行Skill | skill={name} | flow_id={flow_id}")
            skill_inst = self._get_skill(name)
            skill_inst.set_flow_id(flow_id)
            skill_state_manager.update_skill_start(flow_id, name)
            try:
                res = skill_inst.run(current_ctx)
                current_ctx.update(res)
                skill_state_manager.update_skill_success(flow_id, name, str(res)[:200])
                logger.info(f"✅ Skill执行成功 | skill={name} | flow_id={flow_id}")
            except Exception as e:
                skill_state_manager.update_skill_failed(flow_id, name, str(e))
                logger.error(f"❌ Skill执行失败 | skill={name} | flow_id={flow_id} | 错误: {str(e)}")
                raise
        return current_ctx

    def run_requirement_review(self, requirement: str, biz_module: str = None) -> Dict[str, Any]:
        """需求评审流程：并行子Skill → 主Skill汇总"""
        flow_id = f"flow_{str(uuid.uuid4())[:8]}"
        slug = output_manager.generate_slug(requirement[:50])

        # 修复点：start_flow 传入 flow_id
        context_manager.start_flow(flow_id, requirement, slug)
        # 通过公开方法写入阶段与业务模块，避免外部直访私有属性
        context_manager.update_context(flow_id, {
            "current_stage": "reviewing",
            "biz_module": biz_module or ""
        })
        logger.info(
            f"🚀 启动需求评审流程 | flow_id={flow_id} | slug={slug} | biz_module={biz_module or '未指定'}"
        )

        # 修复点：检索传入 flow_id
        rag_ctx = self._retrieve_full_context(
            flow_id, requirement, skill_filter="req-review", biz_module=biz_module
        )
        checklist = self._get_review_checklist()

        sub_input = {
            "requirement": requirement,
            "business_context": rag_ctx["business_docs"],
            "skill_context": rag_ctx["skill_examples"],
            "history_context": rag_ctx["history_cases"],
            "checklist": checklist
        }

        sub_skills = ["req-review-associator", "req-review-challenger"]
        parallel_res = self.run_parallel_skills(flow_id, sub_skills, sub_input)

        main_input = {
            "requirement": requirement,
            "association_result": parallel_res.get("req-review-associator", {}).get(
                "association_analysis", "（关联分析未产出结果）"),
            "challenge_result": parallel_res.get("req-review-challenger", {}).get(
                "challenge_result", "（问题挑战未产出结果）"),
            "history_context": rag_ctx["history_cases"]
        }
        logger.info(f"🧩 主Skill汇总开始 | flow_id={flow_id} | skill=req-review-main")
        main_skill = self._get_skill("req-review-main")
        main_skill.set_flow_id(flow_id)
        main_res = main_skill.run(main_input)

        review_report = main_res["final_review_report"]
        # 修复点：传入 flow_id
        context_manager.set_review_report(flow_id, review_report)
        output_manager.save_file(
            slug, "review_report", review_report,
            module=biz_module or "", page="", requirement=requirement
        )
        logger.info(f"✅ 需求评审完成 | flow_id={flow_id} | 报告长度={len(review_report)}字")

        return {
            "flow_id": flow_id,
            "slug": slug,
            "review_report": review_report,
            "stage": "reviewed",
            "history_used": len(rag_ctx["history_cases"]) > 0
        }

    def run_review_generation(
            self,
            biz_module: str,
            requirement_desc: str,
            upload_file_bytes: Optional[bytes] = None,
            upload_filename: str = "",
            page: str = None,
            upload_content_text: str = "",
            history_version: str = "",
            history_mode: str = "auto"
    ) -> dict:
        """
        需求评审报告生成【py3.9兼容，基于SKILL.md编排的req-review Skill】
        :param biz_module: 前端选择的业务模块，存入flow上下文
        :param requirement_desc: 需求描述文本（必填）
        :param upload_file_bytes: 上传文件bytes，None=不上传文件（与 upload_content_text 二选一）
        :param upload_filename: 上传文件名称
        :param page: 页面标识；传入后 ctx / 落盘 / 历史召回三处统一按页面归属
                     （历史问题：评审线不认页面，导致产物索引记模块层、基线记页面层，事后补沉淀查不到）
        :param upload_content_text: 前端已解析好的文档文本（与 /review、/generate-direct 同一通道）
        :param history_version: 指定带入评审的历史用例基线版本；空=自动取该 (module,page) 最新版本
        :param history_mode: auto=按模块/页面带入历史用例（取不到则自动按新需求评审）；
                             off=本次不带历史用例（当全新需求评审）

        历史用例为什么要「精简后再注入」：
          基线全文（如 pool 页 13391 字 / 56 条）里「步骤描述 + 前置条件」占一半以上，
          而评审阶段需要的是「既有覆盖了什么」，不是复现路径。精简器（case_condenser）
          按 L1→L2→L3 自动降档，实测 50%→32%→15%，零额外模型调用。
        """
        page = (page or "").strip()
        final_module = (biz_module or "").strip()

        # 1.解析上传文档：优先用前端已解析好的文本（走 /upload 通道），否则解析传入文件
        doc_content = (upload_content_text or "").strip()
        if not doc_content and upload_file_bytes and upload_filename:
            from app.utils.file_parser import extract_document_text
            doc_content = extract_document_text(upload_file_bytes, upload_filename)

        # 2.合成「文档段 + 需求段」——评审线与第二步生成共用同一份文本（compose_requirement）
        composed_requirement = compose_requirement(requirement_desc, doc_content)
        # 备份「未被预算守卫裁剪」的完整版：断点续跑要恢复的是完整输入，
        # 而不是首次评审时可能被裁掉尾部的版本（下面预算守卫会原地改写 composed_requirement）
        composed_requirement_original = composed_requirement

        # 3.写入flow上下文（slug 加随机后缀，避免同模块同秒两次评审互相覆盖上下文与产物）
        slug = f"review_{biz_module}_{int(time.time())}_{random.randint(1000, 9999)}"
        flow_id = f"flow_{slug}"
        context_manager.init_context(flow_id, slug, requirement_desc.strip())
        context_manager.update_context(flow_id, {
            "slug": slug,
            "biz_module": final_module,
            "page": page,
            "requirement_text": requirement_desc.strip(),
            # 合成文本单列一份：第二步生成与检索都用它，避免「评审看得到文档、生成看不到」
            "requirement_full": composed_requirement,
            "upload_doc_text": doc_content,
            "current_stage": "reviewing"
        })

        # 4.业务规则注入：评审线也按 (biz_module, page) 取通用规则 + 模块专属规则作为「评审依据」
        #    （只取规则，不取 history_cases —— 评审线已有独立的历史评审报告召回，避免 token 翻倍）
        #    注意：这里**不加 skill 过滤**，直接复用与用例线完全相同的两份权威规则源，
        #          否则规则文件挂在 req-test-cases 名下，按 req-review 过滤会取不到。
        common_rules = ""
        module_rules = ""
        try:
            common_rules = self.vector_db.get_common_rules() or ""
            if final_module:
                module_rules = self.vector_db.get_module_rules_full(
                    final_module, page=page or None
                ) or ""
        except Exception as e:
            logger.warning(f"⚠️ 评审线业务规则检索失败（不影响评审）: {str(e)}")
        business_rules = f"{common_rules}\n{module_rules}".strip()
        if final_module and not module_rules.strip():
            logger.warning(
                f"⚠️ 评审线未取到模块专属规则（评审将只依据通用规则） | "
                f"biz_module={final_module} | page={page or '模块层'}"
            )

        # 5.查询同模块（+本页）历史评审沉淀资产
        history_reports_list: List[str] = []
        if final_module:
            history_docs = self.vector_db.search_by_module(
                query=f"需求评审 {requirement_desc}",
                module_name=final_module,
                n_results=3,
                asset_type="review_report",
                page=page or None
            )
            history_reports_list = [d["document"] for d in history_docs]
        history_reports_text = "\n\n".join(history_reports_list)

        # 5.5 历史用例召回 + 精简（评审依据：判断本需求的既有覆盖与受影响范围）
        #     未选模块 / history_mode=off → 不带历史用例，直接按新需求评审（不报错、不阻断）
        history_cases: List[Dict[str, Any]] = []
        history_source = "none"
        history_used_version = ""
        history_scope = ""
        condensed = {"text": "", "level": "", "count": 0, "chars": 0}
        if final_module and (history_mode or "auto").strip().lower() != "off":
            try:
                history_cases, history_source, history_used_version, history_scope = \
                    self._recall_review_history_cases(final_module, page, history_version)
            except Exception as e:
                logger.warning(f"⚠️ 评审线历史用例召回失败（不影响评审）: {str(e)}")
            if history_cases:
                condensed = condense_cases(
                    history_cases, "L1",
                    scope_label=history_scope, version=history_used_version
                )
                logger.info(
                    f"📚 评审线召回历史用例 | module={final_module} | page={page or '模块层'} | "
                    f"来源={history_source} | 版本={history_used_version or '未标注'} | "
                    f"{condensed['count']}条 → 精简后 {condensed['chars']}字（级别{condensed['level']}）"
                )
            else:
                logger.info(
                    f"ℹ️ 该模块/页面暂无可召回的历史用例基线，本次按「新需求」评审 | "
                    f"module={final_module} | page={page or '模块层'}"
                )
        elif final_module:
            logger.info(f"ℹ️ history_mode=off，本次评审不带历史用例 | module={final_module}")

        checklist = self._get_review_checklist()

        # 6.输入预算守卫：按「历史评审 → 用例档位 → 业务规则 → 文档」顺序裁剪，直到进入预算。
        #   目的：宁可少带次要上下文，也不让平台因超上下文直接 400（用户看不到任何解释）。
        budget = int(os.getenv("MAX_REVIEW_INPUT_CHARS", str(DEFAULT_MAX_REVIEW_INPUT_CHARS)))
        trimmed: List[Dict[str, Any]] = []
        doc_truncated = False

        def _total_chars() -> int:
            return (len(composed_requirement) + len(business_rules)
                    + len(history_reports_text) + len(condensed.get("text") or "")
                    + len(checklist))

        total = _total_chars()
        if total > budget:
            logger.warning(
                f"✂️ 评审输入超预算，开始裁剪 | 预算={budget}字 | 当前={total}字 | "
                f"（文档={len(composed_requirement)} 规则={len(business_rules)} "
                f"历史评审={len(history_reports_text)} 用例={condensed.get('chars')}）"
            )

            # ① 历史评审：top-3 → 仅最新 1 份 → 全部去掉
            if history_reports_list and _total_chars() > budget:
                before = len(history_reports_text)
                history_reports_list = history_reports_list[:1]
                history_reports_text = "\n\n".join(history_reports_list)
                if _total_chars() > budget:
                    history_reports_list = []
                    history_reports_text = ""
                trimmed.append({"block": "历史评审", "before": before,
                                "after": len(history_reports_text), "action": "精简/去除"})

            # ② 历史用例：L1 → L2 → L3 → 去掉
            while condensed.get("text") and _total_chars() > budget:
                lower = next_level(condensed["level"])
                before = condensed["chars"]
                if not lower:
                    condensed = {"text": "", "level": "", "count": condensed["count"], "chars": 0}
                    trimmed.append({"block": "历史用例", "before": before, "after": 0,
                                    "action": "超出预算，本次不注入"})
                    break
                condensed = condense_cases(history_cases, lower,
                                           scope_label=history_scope, version=history_used_version)
                trimmed.append({"block": "历史用例", "before": before, "after": condensed["chars"],
                                "action": f"降档至 {lower}"})

            # ③ 业务规则：先去「模块规则」（页规则全文很长），**通用规则留到最后一档**
            if business_rules and _total_chars() > budget:
                before = len(business_rules)
                business_rules = (common_rules or "").strip()
                trimmed.append({"block": "业务规则(模块/页规则)", "before": before,
                                "after": len(business_rules), "action": "去除模块规则，仅保留通用规则"})

            # ④ 文档：截断（文档是评审的主要依据，截断意味着信息丢失）
            #    注意：截断后会追加提示语，allow 需预留提示语长度，否则会因几十字溢出误触发第⑤档
            if _total_chars() > budget:
                fixed = len(business_rules) + len(history_reports_text) + len(condensed.get("text") or "") + len(checklist)
                allow = max(budget - fixed - len(_DOC_TRUNC_NOTICE), 2000)
                if len(composed_requirement) > allow:
                    before = len(composed_requirement)
                    composed_requirement = composed_requirement[:allow] + _DOC_TRUNC_NOTICE
                    doc_truncated = True
                    trimmed.append({"block": "需求/文档", "before": before,
                                    "after": len(composed_requirement), "action": "按剩余预算截断"})
                    logger.warning(
                        f"⚠️ 需求/文档超预算被截断 | 原 {before} 字 → {len(composed_requirement)} 字"
                    )

            # ⑤ 通用规则：**最后手段** —— 文档已按剩余预算截断仍超预算，才牺牲通用规则
            #    （通用规则体积小、是强制约束，价值高于超长文档的尾部）
            if business_rules and _total_chars() > budget:
                before = len(business_rules)
                business_rules = ""
                trimmed.append({"block": "通用规则", "before": before, "after": 0,
                                "action": "最后手段：文档已截断仍超预算，去除通用规则"})

            logger.warning(
                f"✂️ 评审输入裁剪完成 | {total}字 → {_total_chars()}字（预算 {budget}字）| "
                f"明细={trimmed}"
            )

        # 7.组装输入上下文，交给 req-review 编排型Skill（内部调度 associator + challenger → main 汇总）
        skill_input = {
            "requirement_text": composed_requirement,
            # 已合成进 requirement_text，这里传空避免子Skill把文档段拼两遍
            "doc_content": "",
            "history_reports": history_reports_text,
            # 历史用例精简清单：只给 associator / challenger（main 用两子的结论，不重复注入）
            "history_cases_condensed": condensed.get("text") or "",
            "business_rules": business_rules,
            "biz_module": final_module,
            "page": page,
            "checklist": checklist
        }

        logger.info(
            f"📥 调用 req-review 编排Skill | flow_id={flow_id} | slug={slug} | "
            f"page={page or '模块层'} | 规则依据={len(business_rules)}字 | "
            f"文档={len(doc_content)}字 | 历史评审={len(history_reports_text)}字 | "
            f"历史用例={condensed.get('chars') or 0}字({condensed.get('level') or '未带'})"
        )
        review_skill = self._get_skill("req-review")
        review_skill.set_flow_id(flow_id)
        review_res = review_skill.run(skill_input)
        report_content = review_res["final_review_report"]

        # 8.回写评审报告到上下文，并落盘（页面归属与 ctx 保持一致）
        context_manager.set_review_report(flow_id, report_content)
        output_manager.save_file(
            slug, "review_report", report_content,
            module=final_module, page=page, requirement=requirement_desc.strip()
        )
        # 断点续跑上下文单独落盘：产物索引（_manifest.json）里只留需求前 200 字摘要，
        # 够前端展示但**不够重新生成**。后端重启/隔天续跑时按 slug 从这里恢复完整需求与文档
        # （见 _recover_ctx_from_artifacts），避免第二步拿 200 字需求静默产出对不上的用例。
        output_manager.save_requirement_context(
            slug,
            requirement_text=requirement_desc.strip(),
            requirement_full=composed_requirement_original,
            upload_doc_text=doc_content,
        )
        logger.info(
            f"✅ 评审报告生成完成 | flow_id={flow_id} | page={page or '模块层'} | "
            f"报告长度={len(report_content)}字"
        )

        if history_cases and not condensed.get("text"):
            logger.warning(
                f"⚠️ 历史用例已召回但未注入（预算裁剪或档位用尽）| "
                f"{len(history_cases)}条 | 详见上方裁剪明细"
            )

        return {
            "code": 0,
            "flow_id": flow_id,
            "slug": slug,
            "review_report": report_content,
            "biz_module": final_module,
            "page": page,
            "has_history": bool(history_reports_text),
            "has_upload_doc": bool(doc_content),
            "business_rules_len": len(business_rules),
            # ---- 历史用例带入情况（前端据此提示「带了多少、精简到什么程度」）----
            "history_cases_count": condensed.get("count", 0) if history_cases else 0,
            "history_cases_source": history_source,
            "history_cases_version": history_used_version,
            "history_cases_scope": history_scope,
            "history_cases_level": condensed.get("level") or "",
            "history_cases_condensed_chars": condensed.get("chars") or 0,
            "history_mode": (history_mode or "auto"),
            # ---- 输入裁剪明细（空=未触发；非空说明有内容被瘦身/丢弃，前端应展示）----
            "trimmed": trimmed,
            "doc_truncated": doc_truncated,
            "input_chars": _total_chars(),
            "input_budget": budget,
            # ---- 降级的子Skill（非空说明报告缺少对应章节，前端应提示）----
            "degraded_skills": review_res.get("degraded_skills") or []
        }

    # ==================== 评审线：历史用例召回 ====================
    def _latest_baseline_version(self, module_name: str, page: str) -> str:
        """取该 (module, page) 已沉淀用例基线中的最新版本号；无则返回空串"""
        try:
            from app.utils.metadata_manager import meta_manager
            best = None
            for item in meta_manager.get_module_page_versions(module_name):
                if (item.get("page") or "") != (page or ""):
                    continue
                m = re.match(r"^v?(\d+)\.(\d+)$", (item.get("version") or "").strip())
                if not m:
                    continue
                pair = (int(m.group(1)), int(m.group(2)))
                if best is None or pair > best[0]:
                    best = (pair, item.get("version") or "")
            return best[1] if best else ""
        except Exception as e:
            logger.warning(f"⚠️ 查询最新基线版本失败 | module={module_name} | page={page or '模块层'} | {str(e)}")
            return ""

    def _recall_review_history_cases(
            self,
            module_name: str,
            page: str = None,
            version: str = ""
    ):
        """
        评审线历史用例召回（结果会被精简后再注入，不直接进 prompt）。

        通道优先级（与增量模式第一步的召回策略保持一致）：
          1. 结构化基线 metadata_manager.get_module_version_cases —— 权威、可指定版本、字段结构化；
             页面层取不到时回退模块层基线（用户只选模块时也应能拿到该模块的既有用例）。
          2. 向量库 search_by_module(asset_type=test_cases) —— 兜底（旧数据 / 未写结构化元数据）
          3. 都没有 → 返回空，调用方按「新需求」评审

        :return: (case_list, source, version, scope_label)
        """
        if not module_name:
            return [], "none", "", ""

        from app.utils.metadata_manager import meta_manager

        # 页面层优先，其次模块层（page 为空时只查模块层）
        search_pages = [page or "", ""] if (page or "") else [""]
        seen = set()
        for target_page in search_pages:
            if target_page in seen:
                continue
            seen.add(target_page)
            use_version = (version or "").strip() or self._latest_baseline_version(module_name, target_page)
            if not use_version:
                continue
            rec = meta_manager.get_module_version_cases(module_name, use_version, target_page)
            if rec and rec.get("case_list"):
                scope = f"{module_name} / {target_page or '模块层'}"
                if target_page != (page or ""):
                    logger.info(
                        f"ℹ️ 本页无基线，回退到模块层基线 | module={module_name} | "
                        f"page={page or '模块层'} → {target_page or '模块层'}"
                    )
                return list(rec["case_list"]), "metadata", use_version, scope

        # 兜底：向量库召回整篇基线，现场解析成条目
        try:
            docs = self.vector_db.search_by_module(
                query=f"历史用例基线 {module_name} {page or ''}",
                module_name=module_name,
                n_results=1,
                asset_type="test_cases",
                page=page or None
            )
            if docs:
                cases = cases_from_markdown(docs[0].get("document") or "")
                if cases:
                    logger.info(
                        f"ℹ️ 历史用例走向量库兜底 | module={module_name} | 解析出 {len(cases)} 条"
                    )
                    return cases, "vector", "", f"{module_name} / {page or '模块层'}"
        except Exception as e:
            logger.warning(f"⚠️ 历史用例向量兜底失败: {str(e)}")

        return [], "none", "", ""

    def _recover_ctx_from_artifacts(self, slug: str, biz_module: str = None, page: str = None) -> Optional[str]:
        """
        后端重启 / 隔天续跑时，内存上下文已丢失，但评审报告其实已落盘在
        report/{slug}-review_report.md。此处按 slug 从「产物文件 + _manifest.json」
        重建最小可用上下文，让第二步继续跑，不必白重跑一次评审（3 次 LLM 调用）。

        重建字段：requirement_text / requirement_full / upload_doc_text / review_report /
                biz_module / page / current_stage
        返回重建出的 flow_id；无法重建（无评审产物）时返回 None。
        """
        report = output_manager.get_asset_file(slug, "review_report") or ""
        if not report.strip():
            logger.warning(f"⚠️ 未找到落盘的评审产物，无法恢复上下文 | slug={slug}")
            return None

        meta = output_manager.get_asset_meta(slug, "review_report") or {}
        rec_module = (biz_module or meta.get("module") or "").strip()
        rec_page = (page or meta.get("page") or "").strip()

        # 完整需求上下文优先取自 _meta 里的续跑快照（索引里只有 200 字摘要）；
        # 快照缺失（旧产物）时降级为索引摘要，并明确告警 —— 否则会静默拿 200 字需求生成用例
        ctx_saved = output_manager.load_requirement_context(slug) or {}
        requirement = (ctx_saved.get("requirement_text") or meta.get("requirement") or "").strip()
        requirement_full = (ctx_saved.get("requirement_full") or "").strip()
        upload_doc_text = (ctx_saved.get("upload_doc_text") or "").strip()

        if not requirement_full:
            logger.warning(
                f"⚠️ 断点续跑：未找到完整需求上下文（_meta 快照缺失，可能是旧产物），"
                f"将只用需求摘要（{len(requirement)}字）生成，产物可能与原始需求不符 | slug={slug}"
            )

        flow_id = f"flow_{slug}"
        context_manager.init_context(flow_id, slug, requirement or slug)
        context_manager.update_context(flow_id, {
            "slug": slug,
            "biz_module": rec_module,
            "page": rec_page,
            "requirement_text": requirement,
            "requirement_full": requirement_full,
            "upload_doc_text": upload_doc_text,
            "review_report": report,
            "current_stage": "reviewed",
            "recovered_from_artifact": True
        })
        logger.info(
            f"♻️ 已从落盘产物恢复上下文（后端重启/隔天续跑） | slug={slug} | "
            f"module={rec_module or '未指定'} | page={rec_page or '模块层'} | "
            f"评审报告={len(report)}字 | 需求摘要={len(requirement)}字 | "
            f"合成需求={len(requirement_full)}字 | 文档段={len(upload_doc_text)}字"
        )
        return flow_id

    # ==================== 用例质量评审（case-quality-scorer 挂载点）====================
    def run_case_scoring(
            self,
            slug: str = "",
            module_name: str = "",
            page: str = "",
            version: str = "",
            use_engine_b: bool = True
    ) -> Dict[str, Any]:
        """
        用例质量评审（引擎 A 必跑、引擎 B 可跳过），两种模式：

        ① slug 模式（生成后沉淀前 / 存量产物补评）：
           评 outputs/{slug}-test_cases.md 产物文件；模块/页面/版本取自产物索引。
        ② 基线模式（产物文件已删、只留结构化基线时）：
           传 module_name + version（+ page），评 metadata 里的基线 md_content。

        产出三样（互相同源）：
          - Markdown 评审报告 → report/{slug}-case_review_report.md
          - 评分 sidecar → metadata/modules/{module}_case_scores.json（键与版本基线同构）
          - 内存结果 → 返回给前端展示（含 suggest_delete 清单，删除须人工确认后才生效）
        """
        from app.utils.metadata_manager import meta_manager
        from app.utils.case_score_store import case_score_store

        page = (page or "").strip()
        module = (module_name or "").strip()
        version = (version or "").strip()

        # 1.取用例内容
        if slug:
            content = output_manager.get_asset_file(slug, "test_cases") or ""
            if not content.strip():
                logger.error(f"❌ 用例评审：未找到产物 | slug={slug}")
                raise Exception(f"未找到用例产物：{slug}")
            meta = output_manager.get_asset_meta(slug, "test_cases") or {}
            module = module or (meta.get("module") or "").strip()
            page = page or (meta.get("page") or "").strip()
            version = version or (meta.get("version") or "").strip() or "v1.0"
            source_desc = f"产物 {slug}"
        else:
            if not module or not version:
                raise Exception("基线评审模式必须传 module_name 与 version")
            record = meta_manager.get_module_version_cases(module, version, page=page or None)
            if not record:
                raise Exception(f"未找到基线：{module} / {page or '模块层'} / {version}")
            content = record.get("md_content") or ""
            source_desc = f"结构化基线 {module} / {page or '模块层'} / {version}"
            slug = f"score_{module}_{page or 'all'}_{version}_{int(time.time())}"

        # 2.结构化解析（复用统一解析链，动态表头）
        cases = meta_manager.parse_markdown_cases(content)
        if not cases:
            raise Exception("用例解析失败：产物中未找到有效的 9 列用例表")

        # 2.1 读产物里的分片映射（方案 B）：给引擎 B「按编号归属装箱」用，
        #     避免同一编号的用例被顺序切片拆到两片 ⇒ B3/B4 打分片间漂移。
        #     条数校验在 segment_map.load 内做：产物被编辑过则行号失效，宁可退回顺序分片。
        from app.utils import segment_map
        seg_payload = segment_map.load(content, case_count=len(cases))
        rule_rows = (seg_payload or {}).get("rule_rows") or {}
        seg_stats = segment_map.marker_stats(seg_payload)
        logger.info(
            f"🧩 评审分片映射 | segment_map={'命中' if seg_stats.get('present') else '未命中'}"
            f"（{'按编号归属装箱' if rule_rows else '回退按条数顺序分片'}） | "
            f"归属编号={len(rule_rows)}个 | 归属不明={seg_stats.get('unassigned', 0)}条"
            f"{' | 来源=' + str(seg_stats.get('source')) if seg_stats.get('present') else ''}"
        )

        logger.info(
            f"🧪 启动用例质量评审 | slug={slug} | module={module or '未指定'} | "
            f"page={page or '模块层'} | version={version} | 用例={len(cases)}条 | "
            f"引擎B={'开' if use_engine_b else '关'}"
        )

        # 3.评审（附上次评分做分数漂移检测）
        scorer = self._get_skill("case-quality-scorer")
        scorer.set_flow_id(f"flow_{slug}")
        prev_scores = case_score_store.get_scores(module, page, version) if module else None
        result = scorer.score_cases(
            cases, module=module, page=page,
            use_engine_b=use_engine_b, prev_scores=prev_scores,
            rule_rows=rule_rows,
        )

        # 4.评审报告落盘 report/（报告类资产统一目录，与用例产物分开）
        report_md = scorer.render_report(result, module, page, version, source_desc)
        report_path = output_manager.save_file(
            slug, "case_review_report", report_md,
            module=module, page=page, version=version, requirement=source_desc,
        )

        # 5.评分 sidecar（不动 9 列契约，键与版本基线同构）
        if module:
            try:
                cases_payload = {
                    r["norm"]: {
                        "name": r["name"],
                        "total": r["total"],
                        "dims": r["dims"],
                        "a_deduction": r["a_deduction"],
                        "hard_issues": r["hard_issues"],
                        "automatability": r["automatability"],
                        "fixture_pattern": r["fixture_pattern"],
                        "decision": r["decision"],
                        "reason": r["reason"],
                        "engine_b_scored": r["engine_b_scored"],
                    }
                    for r in result["cases"]
                }
                case_score_store.save_scores(
                    module, page, version, cases_payload,
                    engine_b=use_engine_b, source_slug=slug,
                    report_file=os.path.basename(report_path),
                )
            except Exception as e:
                # sidecar 失败不影响报告与返回（评审报告已落盘，是后备真相源）
                logger.error(f"❌ 评分 sidecar 保存失败（不影响评审报告）: {str(e)}")

        return {
            "code": 0,
            "slug": slug,
            "module": module,
            "page": page,
            "version": version,
            "engine_b": bool(use_engine_b),
            "summary": result["summary"],
            "cases": result["cases"],
            "failed_segments": result["failed_segments"],
            "unmapped_names": result["unmapped_names"],
            "packing": result.get("packing") or {},
            "segment_map": seg_stats,
            "report_file": os.path.basename(report_path),
        }

    def apply_score_decisions(
            self,
            module_name: str,
            page: str,
            base_version: str,
            removed_names: List[str],
            reasons: Dict[str, str] = None
    ) -> Dict[str, Any]:
        """
        存量基线的评审处置：人工确认的差评用例从**新版本基线**中移除。

        纪律（与增量删除闸门同款）：
          - 不物理删旧版本——旧基线键保留可追溯；
          - 新版本 = 旧基线剔除确认用例后的内容，版本号取 next_version；
          - 向量库只留最新（precipitate_to_project 内部先 add 后删旧）；
          - 删除原因回写评分 sidecar（记在被评版本上，留审计痕）。
        """
        from app.utils.metadata_manager import meta_manager
        from app.utils.case_score_store import case_score_store
        from app.scripts.case_quality_scorer import remove_cases_from_markdown
        from app.scripts.req_test_cases import ReqTestCases

        module = (module_name or "").strip()
        page = (page or "").strip()
        if not module:
            raise Exception("缺少业务模块")
        if not removed_names:
            return {"code": 1, "message": "未传入要移除的用例", "removed": 0}

        record = meta_manager.get_module_version_cases(module, base_version, page=page or None)
        if not record:
            raise Exception(f"未找到基线：{module} / {page or '模块层'} / {base_version}")
        md_content = record.get("md_content") or ""

        new_md, removed_count = remove_cases_from_markdown(md_content, removed_names)
        if removed_count == 0:
            logger.warning(
                f"⚠️ 评审处置：没有匹配到要移除的用例 | module={module} | "
                f"page={page or '模块层'} | version={base_version} | 传入={len(removed_names)}个名称"
            )
            return {"code": 1, "message": "基线中没有匹配到要移除的用例（名称可能对不上）", "removed": 0}

        new_version = meta_manager.get_next_version(module, page)
        slug = f"review_prune_{module}_{page or 'all'}_{int(time.time())}"
        reason_text = f"评审删除{removed_count}条差评用例（基线{base_version}→{new_version}）"
        msg = output_manager.precipitate_to_project(
            slug=slug,
            asset_type="test_cases",
            content=new_md,
            requirement=reason_text,
            module_name=module,
            version=new_version,
            page=page,
        )

        # 删除原因回写评分 sidecar（记在被评版本上，可追溯谁被删、为什么删）
        try:
            reasons = reasons or {}
            decisions = {
                ReqTestCases._norm_case_name(n): {
                    "decision": "deleted",
                    "reason": reasons.get(n) or f"评审建议删除（{base_version}→{new_version}）",
                }
                for n in removed_names
            }
            case_score_store.update_decisions(module, page, base_version, decisions)
        except Exception as e:
            logger.warning(f"⚠️ 删除决策回写失败（不影响基线处置）: {str(e)}")

        logger.info(
            f"✅ 评审处置完成 | module={module} | page={page or '模块层'} | "
            f"移除={removed_count}条 | 新基线={new_version}（旧版 {base_version} 保留可追溯）"
        )
        return {
            "code": 0,
            "message": msg,
            "removed": removed_count,
            "base_version": base_version,
            "new_version": new_version,
        }

    def run_test_case_generation(self, slug: str, biz_module: str = None, page: str = None) -> Dict[str, Any]:
        """用例生成流程（串行）"""
        # 通过slug反查flow_id（改用公开方法，避免外部直访私有属性）
        flow_id = context_manager.find_by_slug(slug)

        # 允许生成用例的阶段：
        #   reviewed  —— 评审完成后首次生成
        #   completed —— 已生成过用例（快速模式跳过评审，首次生成后即为completed）
        # 若不放开 completed，快速模式下点击「补充规则重新生成」会误报「请先完成需求评审」
        allowed_stages = ("reviewed", "completed")
        ctx = context_manager.get_context(flow_id) if flow_id else None

        if not flow_id or not ctx or ctx.get("current_stage") not in allowed_stages:
            # 断点续跑：先尝试从落盘产物恢复上下文，恢复不了才报错
            logger.warning(
                f"⚠️ 未命中可用的内存上下文，尝试从产物恢复 | slug={slug} | "
                f"flow_id={flow_id or 'None'} | 当前阶段={ctx.get('current_stage') if ctx else 'None'}"
            )
            flow_id = self._recover_ctx_from_artifacts(slug, biz_module, page)
            ctx = context_manager.get_context(flow_id) if flow_id else None
            if not flow_id or not ctx:
                logger.error(f"❌ 未找到slug对应的流程且无法从产物恢复 | slug={slug}")
                raise Exception("请先完成需求评审（未找到该需求的评审上下文与评审产物）")

        # 业务模块优先级：入参 > 上下文留存（完整模式下前端可能未回传模块）
        final_module = (biz_module or ctx.get("biz_module") or "").strip()
        # 页面优先级：入参 > 上下文留存；空=模块层
        final_page = (page or ctx.get("page") or "").strip()
        context_manager.update_context(flow_id, {
            "current_stage": "generating",
            "biz_module": final_module,
            "page": final_page
        })
        # 生成用的需求文本 =「上传文档段 + 需求段」（与评审线同一份，由 compose_requirement 统一合成）。
        # 老流程 ctx 里没有 requirement_full 时，用留存的 requirement_text + upload_doc_text 现场合成。
        requirement_text = (ctx.get("requirement_text") or "").strip()
        requirement_full = (ctx.get("requirement_full") or "").strip() or compose_requirement(
            requirement_text, ctx.get("upload_doc_text", "")
        )
        logger.info(
            f"🚀 启动用例生成流程 | flow_id={flow_id} | slug={slug} | "
            f"biz_module={final_module or '未指定'} | page={final_page or '模块层'} | "
            f"生成用需求={len(requirement_full)}字（其中文档段{len(ctx.get('upload_doc_text', '') or '')}字）"
        )

        rag_ctx = self._retrieve_full_context(
            flow_id, requirement_full, skill_filter="req-test-cases",
            biz_module=final_module or None, page=final_page or None
        )
        supplement_text = "；".join(ctx["supplement_rules"]) if ctx["supplement_rules"] else ""

        test_skill = self._get_skill("req-test-cases")
        test_skill.set_flow_id(flow_id)
        res = test_skill.run({
            "requirement": requirement_full,
            "review_report": ctx["review_report"],
            "business_context": rag_ctx["business_docs"],
            "common_rules": rag_ctx["common_rules"],
            "module_rules": rag_ctx["module_rules"],
            "skill_context": rag_ctx["skill_examples"],
            "history_context": rag_ctx["history_cases"],
            "supplement_rules": supplement_text,
            "biz_module": final_module
        })

        test_cases = res["test_cases"]
        # 修复点：传入 flow_id
        context_manager.set_test_cases(flow_id, test_cases)
        output_manager.save_file(
            slug, "test_cases", test_cases,
            module=final_module, page=final_page, requirement=requirement_text
        )
        file_path = output_manager.get_asset_file_path(slug, "test_cases")
        logger.info(
            f"✅ 用例生成完成 | flow_id={flow_id} | module={final_module or '未指定'} | "
            f"page={final_page or '模块层'} | 用例长度={len(test_cases)}字 | 产物={file_path}"
        )

        return {
            "flow_id": flow_id,
            "slug": slug,
            "test_cases": test_cases,
            "file_path": file_path,
            "stage": "completed",
            "history_used": len(rag_ctx["history_cases"]) > 0
        }

    def run_direct_case_generation(self, requirement: str, biz_module: str = None, page: str = None) -> Dict[str, Any]:
        """
        快速模式：跳过评审，直接生成测试用例
        """
        flow_id = f"flow_{str(uuid.uuid4())[:8]}"
        slug = output_manager.generate_slug(requirement[:50])
        final_page = (page or "").strip()

        # 修复点：正确传参 start_flow
        context_manager.start_flow(flow_id, requirement, slug)
        context_manager.update_context(flow_id, {
            "current_stage": "generating",
            "biz_module": biz_module or "",
            "page": final_page
        })
        logger.info(
            f"🚀 启动快速模式用例生成 | flow_id={flow_id} | slug={slug} | "
            f"biz_module={biz_module or '未指定'} | page={final_page or '模块层'}"
        )

        rag_ctx = self._retrieve_full_context(
            flow_id, requirement, skill_filter="req-test-cases", biz_module=biz_module,
            page=final_page or None
        )
        supplement_text = ""

        test_skill = self._get_skill("req-test-cases")
        test_skill.set_flow_id(flow_id)
        res = test_skill.run({
            "requirement": requirement,
            "review_report": "【快速模式】跳过需求评审，直接基于业务规则生成",
            "business_context": rag_ctx["business_docs"],
            "common_rules": rag_ctx["common_rules"],
            "module_rules": rag_ctx["module_rules"],
            "skill_context": rag_ctx["skill_examples"],
            "history_context": rag_ctx["history_cases"],
            "supplement_rules": supplement_text,
            "biz_module": biz_module or ""
        })

        test_cases = res["test_cases"]
        context_manager.set_test_cases(flow_id, test_cases)
        output_manager.save_file(
            slug, "test_cases", test_cases,
            module=biz_module or "", page=final_page, requirement=requirement
        )
        file_path = output_manager.get_asset_file_path(slug, "test_cases")
        logger.info(
            f"✅ 快速模式用例生成完成 | flow_id={flow_id} | module={biz_module or '未指定'} | "
            f"page={final_page or '模块层'} | 用例长度={len(test_cases)}字 | 产物={file_path}"
        )

        return {
            "flow_id": flow_id,
            "slug": slug,
            "test_cases": test_cases,
            "file_path": file_path,
            "stage": "completed",
            "mode": "direct",
            "history_used": len(rag_ctx["history_cases"]) > 0
        }

    def run_case_diff_flow(
            self,
            module_name: str,
            old_version: str,
            new_version: str,
            new_requirement: str,
            change_desc: str
    ) -> Dict[str, Any]:
        """需求变更完整流程：召回历史 → 差异分析"""
        from app.utils.metadata_manager import meta_manager

        old_version_data = meta_manager.get_module_version_cases(module_name, old_version)
        if not old_version_data:
            raise Exception(f"未找到模块【{module_name}】历史版本 {old_version} 用例")
        old_cases_md = old_version_data["md_content"]

        flow_id = f"flow_{str(uuid.uuid4())[:8]}"
        slug = output_manager.generate_slug(new_requirement[:50])
        context_manager.start_flow(flow_id, new_requirement, slug)

        rag_ctx = self._retrieve_full_context(
            flow_id, new_requirement, skill_filter="req-test-cases", biz_module=module_name
        )
        test_skill = self._get_skill("req-test-cases")
        test_skill.set_flow_id(flow_id)
        temp_res = test_skill.run({
            "requirement": new_requirement,
            "review_report": "本次为需求迭代，沿用原有评审结论",
            "business_context": rag_ctx["business_docs"],
            "common_rules": rag_ctx["common_rules"],
            "module_rules": rag_ctx["module_rules"],
            "skill_context": rag_ctx["skill_examples"],
            "history_context": rag_ctx["history_cases"],
            "supplement_rules": "",
            "biz_module": module_name
        })
        new_cases_md = temp_res["test_cases"]

        diff_skill = self._get_skill("case-diff-analyzer")
        diff_skill.set_flow_id(flow_id)
        diff_input = {
            "old_cases_md": old_cases_md,
            "new_cases_md": new_cases_md,
            "change_desc": change_desc
        }
        diff_res = diff_skill.run(diff_input)

        return {
            "module_name": module_name,
            "old_version": old_version,
            "new_version": new_version,
            "diff_report": diff_res["diff_report"],
            "new_cases": new_cases_md
        }

    def _get_review_checklist(self) -> str:
        """读取MD格式Checklist"""
        # 改用配置中的绝对路径，避免依赖当前工作目录
        checklist_path = os.path.join(
            config.skills_dir, "req-review", "references", "review_checklist_common.md"
        )
        if os.path.exists(checklist_path):
            with open(checklist_path, "r", encoding="utf-8") as f:
                content = f.read()
            logger.debug(f"📋 加载评审Checklist | 路径={checklist_path} | 长度={len(content)}字")
            return content
        logger.warning(f"⚠️ 评审Checklist不存在 | 路径={checklist_path}")
        return "暂无评审Checklist"

    def promote_to_general_skill(self, slug: str, asset_type: str, skill_name: str):
        """资产升级为通用技能"""
        # 查找对应slug的流程上下文（改用公开方法）
        ctx = context_manager.get_context_by_slug(slug)
        if not ctx:
            logger.error(f"❌ 流程不存在，无法升级为通用技能 | slug={slug}")
            raise Exception("流程不存在")

        content = ctx.get("review_report", "") if asset_type == "review_report" else ctx.get("test_cases", "")
        if not content:
            logger.error(f"❌ 无内容可沉淀 | slug={slug} | asset_type={asset_type}")
            raise Exception("没有可沉淀的内容")
        logger.info(
            f"⬆️ 资产升级为通用技能 | slug={slug} | asset_type={asset_type} | "
            f"skill={skill_name} | 内容长度={len(content)}字"
        )
        skill_dir = os.path.join(config.skills_dir, skill_name, "references")
        os.makedirs(skill_dir, exist_ok=True)
        filename = f"precipitated_{asset_type}_{slug}.md"
        file_path = os.path.join(skill_dir, filename)

        with open(file_path, "w", encoding="utf-8") as f:
            f.write(f"# 沉淀自流程：{slug}\n## 原始需求：{ctx['requirement_text']}\n\n{content}")

        from app.services.plugin_loader import load_all_skills
        load_all_skills()
        return {"message": f"已成功沉淀为通用技能：{filename}"}

    def _recall_history_cases(
            self,
            module_name: str,
            change_description: str,
            version: str = None,
            page: str = None
    ):
        """
        召回模块历史用例（影响面分析与增量生成共用）

        优先取结构化元数据中的指定版本，未指定则回退到向量库语义检索。
        返回 (history_cases_text, used_version)
        """
        history_cases_text = ""
        used_version = version or ""

        if used_version:
            try:
                from app.utils.metadata_manager import meta_manager
                # 必须带 page 查询：否则同模块下不同页面的基线会互相命中（跨页串味）
                version_data = meta_manager.get_module_version_cases(
                    module_name, used_version, page=page
                )
                if version_data:
                    history_cases_text = version_data.get("md_content", "")
                    logger.info(
                        f"📚 命中结构化元数据版本 | module={module_name} | "
                        f"page={version_data.get('page') or '模块层'} | version={used_version} | "
                        f"slug={version_data.get('source_slug', '')} | 用例长度={len(history_cases_text)}字"
                    )
                else:
                    logger.warning(
                        f"⚠️ 未找到指定版本元数据（page={page or '模块层'}），回退向量检索 | "
                        f"module={module_name} | version={used_version}"
                    )
            except Exception as e:
                logger.warning(f"⚠️ 读取版本元数据异常，回退向量检索: {str(e)}")

        if not history_cases_text:
            # ⚠️ 走到这里说明「用户所选版本未命中 → 实际用的是向量语义兜底结果」。
            #    必须把 used_version 置空：前端正是靠「实际版本 ≠ 所选版本」来告警的，
            #    若仍原样回传用户所选版本，两者恒等 ⇒ 告警永不触发，
            #    用户会以为自己用的是 v2.0 基线，实际是向量库里的另一份，影响面结论失真且无提示。
            #    口径与评审线 _recall_review_history_cases（未命中即返回 ""）保持一致。
            if used_version:
                logger.warning(
                    f"⚠️ 所选版本未命中，已回退向量语义检索；版本号置空以便前端提示"
                    f"「实际基线 ≠ 所选版本」 | module={module_name} | "
                    f"page={page or '模块层'} | 所选版本={used_version}"
                )
            used_version = ""
            history_docs = self.vector_db.search_by_module(
                query=f"测试用例 {change_description}",
                module_name=module_name,
                n_results=3,
                page=page
            )
            history_cases_text = "\n".join([doc["document"] for doc in history_docs])
            logger.info(
                f"📚 向量库召回历史用例 | module={module_name} | page={page or '模块层'} | "
                f"命中={len(history_docs)}条 | 长度={len(history_cases_text)}字"
            )

        if not history_cases_text.strip():
            logger.warning(
                f"⚠️ 模块【{module_name}】页面【{page or '模块层'}】无历史用例沉淀，"
                f"增量生成退化为全量生成，建议先沉淀一版基线用例"
            )

        return history_cases_text, used_version

    # ============ 疑点闭环辅助 ============
    @staticmethod
    def _parse_pending_questions(impact_text: str) -> List[Dict[str, str]]:
        """
        从影响面分析结果的「四、需用户确认的疑点」表格中解析疑点列表。

        期望格式（列名固定）：
        | 编号 | 疑点 | 建议默认处理 |
        | Q1 | ... | ... |

        容错：表格缺失 / 本节为「无」/ 行格式不符 → 跳过该行；整体无法解析则返回空列表。
        """
        questions: List[Dict[str, str]] = []
        if not impact_text:
            return questions
        for line in impact_text.splitlines():
            s = line.strip()
            if not s.startswith("|"):
                continue
            cells = [c.strip() for c in s.strip("|").split("|")]
            if len(cells) < 2:
                continue
            # 只认「Q + 数字」编号行，天然跳表头与分隔行
            if not re.match(r"^Q\d+$", cells[0], flags=re.IGNORECASE):
                continue
            questions.append({
                "id": cells[0].upper(),
                "question": cells[1],
                "default": cells[2] if len(cells) > 2 else "",
            })
        return questions

    @staticmethod
    def _format_clarifications(items: List[Dict[str, str]]) -> str:
        """把历史澄清知识格式化为注入 prompt 的文本（只取有答复的条目）"""
        lines = []
        for it in items or []:
            answer = (it.get("answer") or "").strip()
            if not answer:
                continue
            lines.append(f"- {it.get('question', '').strip()} → {answer}")
        return "\n".join(lines)

    def run_impact_analysis(
            self,
            module_name: str,
            change_description: str,
            page: str = None,
            version: str = None
    ) -> Dict[str, Any]:
        """
        增量模式【第一阶段】影响面分析：
        调用 req-review-associator 分析变更影响范围，产出「受影响模块/受影响历史用例/
        上下游接口/建议回归范围」，交由用户确认后再进入第二阶段生成增量用例。

        :param version: 前端选定的历史基线版本号；传入后走元数据**精确取全文**作为分析基线
                        （不传 = 回退向量语义召回，只在「该页只有一个版本」时结果才等价）

        同时：
        - 注入该 (module, page) 历史已澄清的疑点答复，避免重复提问（疑点闭环的复用侧）；
        - 从结果中解析出结构化 pending_questions，供前端逐条答复。
        """
        logger.info(
            f"🧭 影响面分析开始 | module={module_name} | page={page or '模块层'} | "
            f"history_version={version or '未选（走向量兜底）'} | "
            f"变更描述长度={len(change_description)}字"
        )

        history_cases_text, used_version = self._recall_history_cases(
            module_name, change_description, version, page
        )
        logger.info(
            f"📚 影响面分析基线 | module={module_name} | page={page or '模块层'} | "
            f"版本={used_version or '未指定'} | 基线长度={len(history_cases_text)}字 | "
            f"{'元数据精确' if version else '向量兜底'}"
        )

        # 历史澄清知识注入：同一页面已确认过的疑点不再重复问
        known_text = ""
        try:
            from app.utils.metadata_manager import meta_manager
            known_items = meta_manager.get_clarifications(module_name, page or "")
            known_text = self._format_clarifications(known_items)
        except Exception as e:
            logger.warning(f"⚠️ 读取历史澄清知识失败（不影响分析）: {str(e)}")

        skill = self._get_skill("req-review-associator")
        prompt = skill.get_impact_prompt(
            module_name, change_description, history_cases_text, known_text
        )
        logger.info("🤖 调用大模型进行影响面分析")
        # 走 call_with_guard 而非 _call_llm：复用 Skill 声明的超时与重试
        # （直调 _call_llm 会绕过 BaseSkill 的超时保护，LLM 挂起时同步 worker 被占满）
        raw = skill.call_with_guard(prompt)
        logger.info(f"✅ 影响面分析完成 | 长度={len(raw)}字")

        pending_questions = self._parse_pending_questions(raw)
        logger.info(
            f"❓ 影响面疑点解析完成 | 疑点数={len(pending_questions)} | "
            f"已注入历史澄清={len(known_text.splitlines()) if known_text else 0}条"
        )

        return {
            "impact_analysis": raw,
            "pending_questions": pending_questions,
            "module": module_name,
            "page": page or "",
            "history_version": used_version or "",
            "history_len": len(history_cases_text),
            "has_history": bool(history_cases_text.strip())
        }

    def run_incremental_cases(
            self,
            module_name: str,
            change_description: str,
            version: str = None,
            page: str = None,
            impact_analysis: str = None
    ) -> Dict[str, Any]:
        # slug/flow_id 提前生成：后续规则检索与上下文写入都要用
        slug = f"incremental_{module_name}_{int(time.time())}"
        flow_id = f"flow_{slug}"

        logger.info(
            f"🔄 增量用例生成开始 | module={module_name} | page={page or '模块层'} | "
            f"version={version or '默认最新版'} | 变更描述长度={len(change_description)}字 | "
            f"携带影响面分析={bool(impact_analysis)} | slug={slug}"
        )

        # 1. 召回对应模块/页面的历史全量用例
        history_cases_text, used_version = self._recall_history_cases(
            module_name, change_description, version, page
        )

        # 1.1 注入业务规则全文（通用规则 + 该页面专属规则）
        #     历史问题：增量路径从不调用 _retrieve_full_context，第二步生成完全不读 skill，
        #     导致「改了 skill 也不生效」。此处与完整/快速模式口径对齐。
        rag_ctx = self._retrieve_full_context(
            flow_id, change_description or module_name,
            skill_filter="req-test-cases", biz_module=module_name, page=page
        )
        common_rules = rag_ctx.get("common_rules", "")
        module_rules = rag_ctx.get("module_rules", "")
        logger.info(
            f"📏 增量生成已注入业务规则 | 通用规则={len(common_rules)}字 | "
            f"页面专属规则={len(module_rules)}字"
        )

        # 2. 调用增量生成Skill（修正方法名：_getSkill → _get_skill）
        skill = self._get_skill("req-test-cases")
        prompt = skill.get_incremental_prompt(
            history_cases_text, change_description, impact_analysis,
            common_rules=common_rules, module_rules=module_rules
        )
        logger.info("🤖 调用大模型生成增量用例（已注入业务规则）")
        # 同样走 call_with_guard：复用超时/重试/截断直抛（LLMTruncationError 不再被吞）
        raw_result = skill.call_with_guard(prompt)
        logger.info(f"✅ 增量用例生成完成 | 长度={len(raw_result)}字")

        # 3. 合并全量用例：走「定点归并」而非大模型重写全量
        #    历史问题：原实现把「历史全量 + 增量清单」丢给大模型要求输出**完整新表格**，
        #    单次输出必然撞 max_tokens 上限（池页基线 56 条单独就占 8192 上限的 98%），
        #    实测合并后 60 条时末行被截断（缺后 3 列）、4 条新增只落 2 条、编号自检表整个丢失。
        #    现改为：代码做定点归并（新增直接追加 / 删除按名剔除 / 修改按名替换），
        #    只有「修改项与基线用例名对不上」时才请大模型做一次定位（输入仅名称清单，输出极小）。
        #    ⇒ 输出量与「变更条数」成正比、与用例总数无关，用例涨到几百条也不会截断。
        logger.info("🧮 开始合并全量用例 | 模式=定点归并（代码归并为主，模型仅兜底定位）")
        merged_result, merge_meta = skill.merge_full_cases(
            history_cases=history_cases_text,
            incremental_cases=raw_result,
            module_rules=module_rules,
        )
        logger.info(
            f"✅ 全量用例合并完成 | 模式={merge_meta.get('mode')} | "
            f"基线={merge_meta.get('baseline_rows')}条 | 新增={merge_meta.get('added')} | "
            f"修改={merge_meta.get('modified')} | 删除={merge_meta.get('deleted')} | "
            f"未匹配修改={merge_meta.get('unmatched_modified')} | "
            f"丢弃非用例行={merge_meta.get('dropped_rows')} | "
            f"模型兜底调用={merge_meta.get('llm_locate_calls')}次 | "
            f"编号自检表={'已生成' if merge_meta.get('check_table') else '未生成'} | "
            f"列数自检={'通过' if merge_meta.get('columns_ok') else '异常'} | "
            f"长度={len(merged_result)}字"
        )
        # 3.1 删除闸门：本轮默认 hold（一个都不删），把「建议删除」清单交给人工确认
        pending_del = merge_meta.get("pending_deletions") or []
        if pending_del:
            logger.info(
                f"⏸️ 删除确认闸门生效 | 策略={merge_meta.get('deletion_policy')} | "
                f"模型建议删除={len(pending_del)}条（本轮已全部保留，待人工确认） | "
                f"清单={[it.get('name') for it in pending_del]}"
            )
        if merge_meta.get("skipped_duplicates"):
            logger.warning(
                f"⚠️ 新增节重名被跳过={merge_meta['skipped_duplicates']}（模型可能误把修改项放进新增节）"
            )
        if merge_meta.get("unmatched_names"):
            logger.warning(
                f"⚠️ 修改项未能定位到基线（已降级为新增）= {merge_meta['unmatched_names']}"
            )

        # 3.2 缓存本次归并输入 + 落盘待确认删除清单
        #     供「人工勾选确认删除」时用同一份输入二次归并（不重调生成模型）
        self._cache_merge_inputs(slug, {
            "history_cases": history_cases_text,
            "incremental_cases": raw_result,
            "module_rules": module_rules,
            "module": module_name,
            "page": page or "",
            "version": used_version or "",
            "change_description": change_description,
        })
        self._save_merge_snapshot(slug, history_cases_text)
        self._save_pending_deletions(slug, merge_meta)

        # 4. 写入流程上下文，供后续沉淀复用
        # 注意：slug/flow_id 需与返回值一致并回传前端，否则前端按自身生成的 slug
        #       沉淀时 get_context_by_slug 查不到上下文，导致「没有可沉淀的内容」
        try:
            context_manager.init_context(flow_id, slug, change_description)
            context_manager.update_context(flow_id, {
                "slug": slug,
                "biz_module": module_name,
                "page": page or "",
                "requirement_text": change_description,
                "test_cases": merged_result,
                "current_stage": "completed"
            })
        except Exception as e:
            logger.warning(f"⚠️ 增量流程上下文写入失败（不影响生成结果）: {str(e)}")

        # 5. 双口径落盘：
        #    - {slug}-incremental_cases.md：增量清单（仅变更部分），供用户按清单手工修改原文件
        #    - {slug}-test_cases.md：合并后全量，供「重新加载 → 沉淀新版本」
        incremental_file_path = ""
        try:
            output_manager.save_file(
                slug, "incremental_cases", raw_result,
                module=module_name, page=page or "", requirement=change_description
            )
            incremental_file_path = output_manager.get_asset_file_path(slug, "incremental_cases")
            logger.info(f"✅ 增量清单落盘完成 | path={incremental_file_path} | 长度={len(raw_result)}字")
        except Exception as e:
            logger.warning(f"⚠️ 增量清单落盘失败（不影响主流程）: {str(e)}")

        output_manager.save_file(
            slug, "test_cases", merged_result,
            module=module_name, page=page or "", requirement=change_description
        )
        file_path = output_manager.get_asset_file_path(slug, "test_cases")
        logger.info(
            f"✅ 增量全量落盘完成 | module={module_name} | page={page or '模块层'} | "
            f"path={file_path} | 长度={len(merged_result)}字"
        )

        return {
            "flow_id": flow_id,
            "slug": slug,
            "incremental_cases": raw_result,
            "merged_full_cases": merged_result,
            "file_path": file_path,
            "incremental_file_path": incremental_file_path,
            "module": module_name,
            "page": page or "",
            "history_version": used_version,
            "has_history": bool(history_cases_text.strip()),
            # 归并元信息：新增/修改/删除/列数自检结果，供前端与排查用
            "merge_meta": merge_meta
        }

    # ==================== 增量「建议删除」确认闸门（hold → 人工勾选 → 二次归并落盘）====================

    @staticmethod
    def _pending_deletions_path(slug: str) -> str:
        # 放 outputs/_meta 下：不是用例产物，不能和产物文件混放
        return output_manager.get_meta_path(slug, "pending_deletions.json")

    def _save_merge_snapshot(self, slug: str, history_cases: str):
        """
        落盘本次归并所用的基线快照。

        为什么需要：人工确认删除后必须用**同一份基线**做二次归并。若只依赖「重新召回」，
        期间一旦有新版本沉淀，二次归并就等于偷偷换了输入（产物会大范围变动且无从察觉）。
        快照随产物自动回收（见 list_asset_files 的惰性 GC）。
        """
        try:
            path = output_manager.get_meta_path(slug, "merge_baseline.md")
            with open(path, "w", encoding="utf-8") as f:
                f.write(history_cases or "")
            logger.info(
                f"🧷 归并基线快照已落盘 | path={path} | 长度={len(history_cases or '')}字"
            )
        except Exception as e:
            logger.warning(f"⚠️ 归并基线快照落盘失败（降级重建将回退为重新召回）: {str(e)}")

    def _load_merge_snapshot(self, slug: str) -> str:
        try:
            path = output_manager.get_meta_path(slug, "merge_baseline.md")
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    return f.read()
        except Exception as e:
            logger.warning(f"⚠️ 读取归并基线快照失败: {str(e)}")
        return ""

    def _cache_merge_inputs(self, slug: str, payload: Dict[str, Any]):
        """
        缓存一次归并的输入，供「人工确认删除」后二次归并复用（同 slug 覆盖，FIFO 限量）。

        为什么要缓存：hold 模式产出的产物是「全部保留」的版本，用户确认删除后必须
        用**同一份**基线 + 增量清单重新归并，才能保证「除了被删的那几条，其余逐字不变」。
        缓存它就能避免重新召回基线、重新调用生成模型。
        """
        if slug in self._merge_inputs:
            try:
                self._merge_inputs_order.remove(slug)
            except ValueError:
                pass
        self._merge_inputs[slug] = payload
        self._merge_inputs_order.append(slug)
        while len(self._merge_inputs_order) > self._MERGE_CACHE_MAX:
            stale = self._merge_inputs_order.pop(0)
            self._merge_inputs.pop(stale, None)
        logger.debug(f"🧷 归并输入已缓存 | slug={slug} | 当前缓存数={len(self._merge_inputs)}")

    def _save_pending_deletions(self, slug: str, merge_meta: Dict[str, Any]):
        """待确认删除清单落盘：① 后端重启后仍可确认；② 留下「谁建议删了什么」的审计痕迹"""
        try:
            payload = {
                "slug": slug,
                "deletion_policy": merge_meta.get("deletion_policy"),
                "applied": merge_meta.get("deletion_policy") == "applied",
                "pending_deletions": merge_meta.get("pending_deletions") or [],
                "deleted_names": merge_meta.get("deleted_names") or [],
                "baseline_rows": merge_meta.get("baseline_rows"),
                "recorded_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            path = self._pending_deletions_path(slug)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            logger.info(
                f"📝 待确认删除清单已落盘 | path={path} | "
                f"建议删除={len(payload['pending_deletions'])}条 | 已执行删除={len(payload['deleted_names'])}条"
            )
        except Exception as e:
            logger.warning(f"⚠️ 待确认删除清单落盘失败（不影响主流程）: {str(e)}")

    def _load_pending_deletions(self, slug: str) -> Dict[str, Any]:
        path = self._pending_deletions_path(slug)
        if not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"⚠️ 读取待确认删除清单失败 | path={path} | 错误: {str(e)}")
            return {}

    def _rebuild_merge_inputs(
            self, slug: str, module_name: str, page: str,
            change_description: str, version: str
    ) -> Dict[str, Any]:
        """缓存丢失（后端重启）时的降级重建：增量清单读盘 + 基线重新召回"""
        logger.warning(f"⚠️ 未命中归并输入缓存，尝试从落盘重建 | slug={slug}")
        incremental_text = ""
        try:
            path = output_manager.get_asset_file_path(slug, "incremental_cases")
            if path and os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    incremental_text = f.read()
                logger.info(f"📂 已从产物读回增量清单 | path={path} | 长度={len(incremental_text)}字")
        except Exception as e:
            logger.warning(f"⚠️ 读回增量清单失败: {str(e)}")
        if not incremental_text.strip():
            raise Exception("归并输入缓存与增量清单产物均不可用，无法执行删除确认（请重新生成增量用例）")

        # 优先用「首次归并时的基线快照」——保证二次归并输入与首次完全一致。
        # 仅当快照也丢失时才重新召回（此时版本可能已变，日志会显式告警）。
        history_text = self._load_merge_snapshot(slug)
        used_version = ""
        if history_text.strip():
            logger.info(
                f"📂 已从快照读回归并基线 | 长度={len(history_text)}字（与首次归并输入一致）"
            )
        else:
            logger.warning(
                "⚠️ 归并基线快照缺失，降级为重新召回 —— 若期间沉淀过新版本，"
                "二次归并的输入将与首次不同，请核对产物"
            )
            if module_name:
                try:
                    history_text, used_version = self._recall_history_cases(
                        module_name, change_description or module_name, version or None, page or None
                    )
                except Exception as e:
                    logger.warning(f"⚠️ 降级重建时重新召回基线失败: {str(e)}")
        if not history_text.strip():
            raise Exception("基线召回失败，无法二次归并（请重新生成增量用例）")
        logger.info(
            f"✅ 归并输入重建完成 | 基线={len(history_text)}字 | 版本={used_version or '-'}"
        )
        return {
            "history_cases": history_text,
            "incremental_cases": incremental_text,
            "module_rules": "",
            "module": module_name,
            "page": page or "",
            "version": used_version or "",
            "change_description": change_description or "",
        }

    def apply_incremental_deletions(
            self,
            slug: str,
            deleted_names,
            module_name: str = "",
            page: str = "",
            change_description: str = "",
            version: str = ""
    ) -> Dict[str, Any]:
        """
        增量删除确认：人工勾选后，用**同一份归并输入**做二次定点归并并覆盖产物。

        语义：`run_incremental_cases` 默认 hold —— 模型「建议删除」的用例不执行删除、
        原样保留在产物里，清单回传前端。用户勾选后调用本方法，只剔除确认过的用例，
        其余用例逐字不变（不是重新生成，也不重调生成模型）。
        """
        logger.info(
            f"✅ 收到增量删除确认请求 | slug={slug} | 提交删除={len(deleted_names or [])}条 | "
            f"module={module_name or '未传'} | page={page or '未传'}"
        )
        if not slug:
            raise Exception("缺少 slug，无法定位本次增量的归并输入")

        inputs = self._merge_inputs.get(slug)
        if not inputs:
            inputs = self._rebuild_merge_inputs(
                slug, module_name, page, change_description, version
            )

        skill = self._get_skill("req-test-cases")
        norm = skill._norm_case_name

        # 越权校验：只允许删除「本次模型建议删除」清单里的用例（防误删、防跨流程操作）
        pending = self._load_pending_deletions(slug)
        pending_items = pending.get("pending_deletions") or []
        if not pending_items and inputs.get("incremental_cases"):
            # 清单落盘缺失时现场重解析（不依赖落盘文件，保证确认动作可用）
            sections = skill._split_incremental_sections(inputs.get("incremental_cases", ""))
            pending_items = skill._extract_deleted_items(
                sections["delete"], skill._parse_case_rows(inputs.get("history_cases", ""))
            )
            logger.info(f"ℹ️ 待确认删除清单缺失，已现场重新解析 | 条数={len(pending_items)}")
        allowed = {norm(it.get("name")) for it in pending_items if it.get("name")}
        if not allowed:
            logger.warning(
                "⚠️ 本次没有可校验的「建议删除」清单，提交的删除项将全部被忽略（产物保持全量）"
            )
        valid_names, rejected = [], []
        for n in (deleted_names or []):
            (valid_names if norm(n) in allowed else rejected).append(n)
        if rejected:
            logger.warning(
                f"⚠️ 以下待删用例不在本次「建议删除」清单内，已忽略（防误删）: {rejected}"
            )
        logger.info(
            f"🗑️ 删除确认明细 | 允许={list(allowed)} | 采纳={valid_names} | 拒绝={rejected}"
        )

        merged_result, merge_meta = skill.merge_full_cases(
            history_cases=inputs.get("history_cases", ""),
            incremental_cases=inputs.get("incremental_cases", ""),
            module_rules=inputs.get("module_rules", ""),
            confirmed_deletions=valid_names,
        )
        logger.info(
            f"✅ 删除确认后二次归并完成 | 策略={merge_meta.get('deletion_policy')} | "
            f"实际删除={merge_meta.get('deleted')}条 {merge_meta.get('deleted_names')} | "
            f"基线保留={max((merge_meta.get('baseline_rows') or 0) - (merge_meta.get('deleted') or 0), 0)}条 | "
            f"新增={merge_meta.get('added')} | 修改={merge_meta.get('modified')} | "
            f"列数自检={'通过' if merge_meta.get('columns_ok') else '异常'} | "
            f"长度={len(merged_result)}字"
        )

        target_module = inputs.get("module") or module_name
        target_page = inputs.get("page") or page
        target_req = inputs.get("change_description") or change_description
        output_manager.save_file(
            slug, "test_cases", merged_result,
            module=target_module, page=target_page, requirement=target_req
        )
        file_path = output_manager.get_asset_file_path(slug, "test_cases")
        logger.info(f"💾 删除确认后产物已覆盖 | path={file_path} | 长度={len(merged_result)}字")

        # 同步上下文：否则后续按 slug 沉淀拿到的仍是「删除前」的版本
        try:
            context_manager.update_context(f"flow_{slug}", {
                "test_cases": merged_result,
                "deletion_policy": merge_meta.get("deletion_policy"),
                "current_stage": "completed",
            })
            logger.info(f"🧩 删除确认后上下文已同步 | flow_id=flow_{slug}")
        except Exception as e:
            logger.warning(f"⚠️ 删除确认后上下文更新失败（不影响产物）: {str(e)}")

        inputs["deletion_policy"] = merge_meta.get("deletion_policy")
        self._cache_merge_inputs(slug, inputs)
        self._save_pending_deletions(slug, merge_meta)

        return {
            "slug": slug,
            "merged_full_cases": merged_result,
            "file_path": file_path,
            "merge_meta": merge_meta,
            "applied_deletions": merge_meta.get("deleted_names") or [],
            "rejected_deletions": rejected,
            "kept_baseline": max(
                (merge_meta.get("baseline_rows") or 0) - (merge_meta.get("deleted") or 0), 0
            ),
        }

    # ==================== 方案C：规则补丁式更新（生成 → 人工确认 → 定点落盘）====================
    def list_rule_files(self, module_name: str, page: str = None) -> Dict[str, Any]:
        """
        列出该 (模块, 页面) 对应的规则文件及其 R/S/F 编号清单。

        用途：① 增量闭环里告诉用户「本次改动会影响哪些规则文件」；
             ② 规则补丁确认界面展示「当前规则有哪些编号」，便于人工判断补丁是否合理。
        """
        from app.utils.rule_patch_applier import rule_patch_applier
        files = rule_patch_applier.locate_rule_files(module_name, page or "")
        out = []
        for f in files:
            try:
                with open(f["path"], "r", encoding="utf-8") as fp:
                    text = fp.read()
                rows = rule_patch_applier.parse_rule_rows(text)
                out.append({
                    "filename": f["filename"],
                    "path": f["path"],
                    "biz_module": f["biz_module"],
                    "page": f["page"],
                    "char_count": len(text),
                    "rule_ids": [r["id"] for r in rows],
                })
            except Exception as e:
                logger.warning(f"⚠️ 读取规则文件失败 | {f['filename']} | {str(e)}")
        logger.info(
            f"📚 列出规则文件 | module={module_name} | page={page or '模块层'} | 文件数={len(out)}"
        )
        return {"code": 0, "data": out, "module": module_name, "page": page or ""}

    def propose_rule_patch(
            self,
            module_name: str,
            page: str,
            change_description: str,
            impact_analysis: str = ""
    ) -> Dict[str, Any]:
        """
        【第一步】基于「规则文件全文 + 变更确认书」生成结构化规则补丁（不落盘）。

        变更确认书 = 变更描述 + 影响面分析（内含用户对疑点的确认答复）。
        生成的补丁必须经人工在界面确认后，才调用 apply_rule_patch 落盘。
        """
        from app.utils.rule_patch_applier import rule_patch_applier

        logger.info(
            f"🩹 规则补丁提案开始 | module={module_name} | page={page or '模块层'} | "
            f"变更描述长度={len(change_description)}字 | 影响面分析={len(impact_analysis or '')}字"
        )

        rule_files = rule_patch_applier.locate_rule_files(module_name, page or "")
        if not rule_files:
            logger.warning(f"⚠️ 未找到规则文件，无法生成补丁 | module={module_name} | page={page}")
            return {
                "code": 1,
                "message": f"未找到 module={module_name} / page={page or '模块层'} 对应的规则文件",
                "patches": [], "rule_files": [],
                "module": module_name, "page": page or ""
            }

        # 规则全文（同一页面通常只有一个文件；多个则拼接，并在 prompt 里注明文件名）
        chunks = []
        for f in rule_files:
            try:
                with open(f["path"], "r", encoding="utf-8") as fp:
                    chunks.append(f"<!-- 文件: {f['filename']} -->\n{fp.read()}")
            except Exception as e:
                logger.warning(f"⚠️ 读取规则文件失败 | {f['filename']} | {str(e)}")
        rules_text = "\n\n".join(chunks)
        logger.info(
            f"📖 规则全文已就绪 | 文件数={len(rule_files)} | 长度={len(rules_text)}字"
        )

        # 已确认疑点答复作为补丁依据
        clar_text = ""
        try:
            from app.utils.metadata_manager import meta_manager
            clar_text = self._format_clarifications(
                meta_manager.get_clarifications(module_name, page or "")
            )
        except Exception as e:
            logger.warning(f"⚠️ 读取澄清知识失败（不影响补丁生成）: {str(e)}")

        change_book = change_description or ""
        if impact_analysis and impact_analysis.strip():
            change_book += "\n\n【影响面分析（含用户确认答复）】\n" + impact_analysis.strip()

        skill = self._get_skill("rule-patch-proposer")
        prompt = skill.get_patch_prompt(
            module_name=module_name, page=page or "",
            rules_text=rules_text, change_desc=change_book, clarifications=clar_text
        )
        logger.info("🤖 调用大模型生成规则补丁")
        resp = skill.run({
            "biz_module": module_name, "page": page or "",
            "rules_text": rules_text, "change_description": change_book,
            "clarifications": clar_text,
        })
        patches = resp.get("patches", [])
        logger.info(
            f"✅ 规则补丁提案完成 | 补丁数={len(patches)} | 解析成功={resp.get('parse_ok')} | "
            f"目标文件={[f['filename'] for f in rule_files]}"
        )

        return {
            "code": 0,
            "message": "ok",
            "summary": resp.get("summary", ""),
            "parse_ok": resp.get("parse_ok", False),
            "patches": patches,
            "rule_files": [f["filename"] for f in rule_files],
            "rule_file_paths": {f["filename"]: f["path"] for f in rule_files},
            "module": module_name,
            "page": page or "",
            "rules_chars": len(rules_text),
        }

    def apply_rule_patch(
            self,
            module_name: str,
            page: str,
            patches: List[Dict[str, Any]],
            selected_ids: Optional[List[str]] = None,
            change_note: str = ""
    ) -> Dict[str, Any]:
        """
        【第二步】把人工确认后的补丁定点应用到规则文件（备份 → 替换 → 打「需求变更改动」标记 → 热重载知识库）。

        :param selected_ids: 前端勾选保留的补丁编号（如 ["p1","p2"]）；
                             None/空 = 全部应用。用于「人工确认闸门」的逐条取舍。
        :param change_note: 本次需求变更描述；会写进规则文件 front matter 的
                             last_change_note，并在规则管理页以「需求变更改动」标注展示。
        """
        from app.utils.rule_patch_applier import rule_patch_applier

        picked = []
        for i, p in enumerate(patches or []):
            pid = str(p.get("_id") or f"p{i+1}")
            if selected_ids is not None and len(selected_ids) > 0 and pid not in selected_ids:
                logger.info(f"⏭️ 跳过未勾选补丁 | id={pid} | op={p.get('op')}")
                continue
            picked.append(p)

        if not picked:
            logger.warning("⚠️ 没有勾选任何补丁，未落盘")
            return {"code": 1, "message": "没有勾选任何补丁，未落盘", "applied": [], "rejected": []}

        rule_files = rule_patch_applier.locate_rule_files(module_name, page or "")
        if not rule_files:
            return {
                "code": 1,
                "message": f"未找到 module={module_name} / page={page or '模块层'} 对应的规则文件",
                "applied": [], "rejected": []
            }

        # 同一页面通常只有一个规则文件；多个时按补丁里的 file 字段分流，缺省落到第一个
        by_file: Dict[str, List[Dict[str, Any]]] = {f["filename"]: [] for f in rule_files}
        default_name = rule_files[0]["filename"]
        for p in picked:
            fname = str(p.get("file") or "").strip()
            if fname in by_file:
                by_file[fname].append(p)
            else:
                by_file[default_name].append(p)

        results = []
        all_applied, all_rejected = [], []
        for f in rule_files:
            plist = by_file.get(f["filename"]) or []
            if not plist:
                continue
            r = rule_patch_applier.apply_to_file(
                f["path"], plist, reload_skills=False, change_note=change_note)
            results.append({
                "filename": f["filename"],
                "ok": r.get("ok"),
                "message": r.get("message"),
                "backup": r.get("backup"),
                "applied": r.get("applied", []),
                "rejected": r.get("rejected", []),
                "diff": r.get("diff", ""),
                "stamp": r.get("stamp", {}),
            })
            all_applied.extend(r.get("applied", []))
            all_rejected.extend(r.get("rejected", []))

        # 全部文件写完后统一热重载一次（非原子，避免多次全量重建）
        loaded = 0
        if all_applied:
            from app.services.plugin_loader import load_all_skills
            loaded = load_all_skills().get("count", 0)
            logger.info(f"🔄 规则补丁落盘后知识库重载 | 文档数={loaded}")

        logger.info(
            f"✅ 规则补丁落盘完成 | module={module_name} | page={page or '模块层'} | "
            f"应用={len(all_applied)} | 拒绝={len(all_rejected)} | 重载文档数={loaded}"
        )
        return {
            "code": 0,
            "message": f"已应用 {len(all_applied)} 条补丁"
                       + (f"，拒绝 {len(all_rejected)} 条" if all_rejected else "")
                       + "，已在规则文件 front matter 标注「需求变更改动」"
                       + (f"，知识库已重载（{loaded} 份文档）" if loaded else ""),
            "applied": all_applied,
            "rejected": all_rejected,
            "files": results,
            "backup_paths": {r["filename"]: r.get("backup") for r in results if r.get("backup")},
            "reloaded": bool(loaded),
            "change_note": change_note or "",
        }


# 全局单例
flow_scheduler = FlowScheduler()
