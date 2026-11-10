# -*- coding: utf-8 -*-
"""
@File    : req_review.py
@Time    : 2026/9/7
@Author  : tanlei
"""
import traceback
import logging
from typing import Dict, Any, List
from concurrent.futures import ThreadPoolExecutor, as_completed

from app.scripts.skill_base import BaseSkill
from app.scripts.skill_state_manager import skill_state_manager
from app.services.llm_adapter import LLMTruncationError

logger = logging.getLogger("SKILL_REQ_REVIEW")


class ReqReview(BaseSkill):
    """
    需求评审编排型Skill（契约来源：app/skills/req-review/SKILL.md）

    工作流：
      1. 并行调用 req-review-associator（关联模块分析）
                 req-review-challenger（Checklist 问题挑战）
      2. 汇总两个子Skill的输出
      3. 调用 req-review-main 生成最终评审报告

    说明：本Skill为「编排型」，不直接产出报告，而是调度子Skill协作完成，
          符合 Harness 插件化 Skill 架构中「Skill可组合、可编排」的设计。
    """

    # 子Skill执行链（供状态上报与前端展示使用）
    SUB_SKILLS = ["req-review-associator", "req-review-challenger"]
    MAIN_SKILL = "req-review-main"
    ALL_SKILLS = SUB_SKILLS + [MAIN_SKILL]

    def __init__(self):
        super().__init__(
            skill_name="req-review",
            priority=1,
            timeout=300,   # 编排链整体兜底超时（子Skill各自超时由自身控制）
            max_retry=1
        )

    # ========== 子Skill懒加载，避免循环导入 ==========
    def _get_sub_skill(self, name: str) -> BaseSkill:
        if name == "req-review-associator":
            from app.scripts.req_review_associator import ReqReviewAssociator
            return ReqReviewAssociator()
        if name == "req-review-challenger":
            from app.scripts.req_review_challenger import ReqReviewChallenger
            return ReqReviewChallenger()
        if name == "req-review-main":
            from app.scripts.req_review_main import ReqReviewMain
            return ReqReviewMain()
        raise ValueError(f"未知子Skill: {name}")

    # ========== 兜底Prompt：子Skill链路异常时降级为单Skill直出 ==========
    def get_prompt(self, input_context: Dict[str, Any]) -> str:
        requirement = input_context.get("requirement_text", "") or input_context.get("requirement", "")
        doc_content = input_context.get("doc_content", "")
        history_reports = input_context.get("history_reports", "")
        business_rules = input_context.get("business_rules", "")
        history_cases = input_context.get("history_cases_condensed", "")

        cases_block = ""
        if history_cases and history_cases.strip():
            cases_block = f"""
4. 该模块/页面的历史用例（已精简，引用时必须逐字照抄用例名称，不得编造）：
{history_cases.strip()}
"""

        return f"""
你是资深测试需求评审专家，请基于以下内容生成标准Markdown评审报告。

## 输入信息
1. 原始需求：
{requirement}

2. 上传文档内容：
{doc_content if doc_content else "（无上传文档）"}

3. 业务规则依据（该模块/页面已沉淀的规则，评审时须逐条核对这些校验点在需求中是否交代清楚）：
{business_rules if business_rules else "（无已沉淀业务规则）"}
{cases_block}
5. 历史相似评审经验：
{history_reports if history_reports else "（无历史沉淀）"}

## 输出要求（严格遵守）
报告必须包含以下五个章节，缺一不可：
## 一、需求概述
## 二、关联模块与受影响范围
## 三、评审发现的问题（表格：序号|问题描述|严重程度(高/中/低)|改进建议）
## 四、改进建议
## 五、评审结论（评审状态：通过/有条件通过/不通过 + 后续行动建议）

约束：禁止编造需求中不存在的功能点；信息不足时标注「待确认」。
"""

    def parse_output(self, raw_response: str) -> Dict[str, Any]:
        return {
            "final_review_report": raw_response,
            "stage": "review_completed"
        }

    # ========== 编排执行入口 ==========
    def run(self, input_context: Dict[str, Any]) -> Dict[str, Any]:
        """
        重写执行入口：并行子Skill → 主Skill汇总
        保留基类的超时/重试语义：子Skill自身具备该能力，编排层负责状态上报与降级
        """
        flow_id = getattr(self, "flow_id", None)
        requirement = input_context.get("requirement_text", "") or input_context.get("requirement", "")
        doc_content = input_context.get("doc_content", "")
        history_reports = input_context.get("history_reports", "")
        biz_module = input_context.get("biz_module", "")
        checklist = input_context.get("checklist", "")
        # 业务规则依据（通用规则 + 模块专属规则全文，由 flow_scheduler 按 (biz_module, page) 精确取回）
        business_rules = input_context.get("business_rules", "")
        # 历史用例精简清单（flow_scheduler 已召回+精简；只给两个子Skill，
        # main 用它们的结论汇总，不重复注入同一份清单 —— 每注入一处就多一份 token）
        history_cases = input_context.get("history_cases_condensed", "")

        logger.info(
            f"🎬 启动需求评审编排链 | flow_id={flow_id} | biz_module={biz_module or '未指定'} | "
            f"需求长度={len(requirement)}字 | 文档长度={len(doc_content)}字 | "
            f"业务规则依据={len(business_rules)}字 | 历史用例清单={len(history_cases)}字"
        )

        # 需求全文 = 上传文档 + 需求描述（文档优先，作为生成依据）
        full_requirement = requirement
        if doc_content.strip():
            full_requirement = (
                f"【用户上传的参考文档内容】\n{doc_content}\n\n【用户需求描述】\n{requirement}"
            )

        # 注册本次编排链，供前端 /api/skill-status 展示
        if flow_id:
            skill_state_manager.init_flow(flow_id, self.ALL_SKILLS)

        # ---------- 1. 并行执行两个子Skill ----------
        sub_input = {
            "requirement": full_requirement,
            "doc_content": doc_content,
            "biz_module": biz_module,
            "business_rules": business_rules,
            "history_cases_condensed": history_cases,
            "history_context": history_reports,
            "checklist": checklist,
        }
        parallel_res, failed_subs = self._run_parallel_sub_skills(flow_id, self.SUB_SKILLS, sub_input)
        if failed_subs:
            logger.warning(
                f"⚠️ 有子Skill未产出结果，报告会缺少对应章节 | flow_id={flow_id} | 失败={failed_subs}"
            )

        # ---------- 2. 主Skill汇总 ----------
        main_input = {
            "requirement": full_requirement,
            "association_result": parallel_res.get("req-review-associator", {}).get(
                "association_analysis", "（关联分析子Skill未产出结果）"),
            "challenge_result": parallel_res.get("req-review-challenger", {}).get(
                "challenge_result", "（问题挑战子Skill未产出结果）"),
            "business_rules": business_rules,
            "history_context": history_reports,
        }

        logger.info(f"🧩 主Skill汇总开始 | flow_id={flow_id} | skill={self.MAIN_SKILL}")
        main_skill = self._get_sub_skill(self.MAIN_SKILL)
        main_skill.set_flow_id(flow_id)
        try:
            if flow_id:
                skill_state_manager.update_skill_start(flow_id, self.MAIN_SKILL)
            main_res = main_skill.run(main_input)
            if flow_id:
                skill_state_manager.update_skill_success(
                    flow_id, self.MAIN_SKILL, main_res.get("final_review_report", "")[:200]
                )
            # 子Skill降级信息随结果回传：接口 / 前端据此提示「报告缺少某章节」，
            # 而不是静默给出一份看起来正常、实际少 1/3 的报告
            if failed_subs:
                main_res["degraded_skills"] = list(failed_subs)
            logger.info(f"✅ 需求评审编排链执行完成 | flow_id={flow_id} | 降级子Skill={failed_subs or '无'}")
            return main_res
        except LLMTruncationError:
            # 输出被截断属**确定性失败**：降级用的 prompt 只会更大，重试必然再失败
            # （白烧 3 次调用，且最终仍以 500 结束）⇒ 直接上抛，让前端看到明确原因。
            logger.error(
                f"❌ 主Skill[{self.MAIN_SKILL}] 输出被截断（finish_reason=length），不再降级重试 | flow_id={flow_id}"
            )
            if flow_id:
                skill_state_manager.update_skill_failed(
                    flow_id, self.MAIN_SKILL, "输出被截断（finish_reason=length）"
                )
            raise
        except Exception as e:
            logger.error(f"❌ 主Skill[{self.MAIN_SKILL}] 执行失败 | flow_id={flow_id} | 错误: {str(e)}")
            logger.error(traceback.format_exc())
            if flow_id:
                skill_state_manager.update_skill_failed(flow_id, self.MAIN_SKILL, str(e))
            # 降级：编排链失败时，用基类能力直出报告，保证流程不中断
            logger.warning(f"⚠️ 编排链降级为单Skill直出 | flow_id={flow_id}")
            fallback_res = super().run({
                "requirement_text": requirement,
                "doc_content": doc_content,
                "business_rules": business_rules,
                "history_cases_condensed": history_cases,
                "history_reports": history_reports,
            })
            # ⚠️ 直出路径只产出 final_review_report，**没有**「二、关联模块与受影响范围」
            #    和「三、评审发现的问题」两章（那是两个子 Skill 的产出）。
            #    必须把降级事实挂回返回值，否则接口回 code:0、前端提示"生成完成"，
            #    用户会把一份少 2/3 章节的报告当成完整评审结论使用。
            if isinstance(fallback_res, dict):
                fallback_res["degraded_skills"] = list(failed_subs) + [self.MAIN_SKILL]
                fallback_res["degraded_reason"] = "orchestration_fallback"
            logger.warning(
                f"⚠️ 已降级为单Skill直出，报告缺少子Skill章节 | flow_id={flow_id} | "
                f"降级项={fallback_res.get('degraded_skills') if isinstance(fallback_res, dict) else '?'}"
            )
            return fallback_res

    # ========== 并行执行子Skill（按优先级排序 + 状态上报） ==========
    def _run_parallel_sub_skills(
            self,
            flow_id: str,
            skill_names: List[str],
            input_ctx: Dict[str, Any]
    ):
        """
        并行执行子Skill。

        :return: (results, failed_names) —— 失败的名字单独返回，
                 让调用方能把「报告缺了哪个章节」如实告诉用户（原来这里是静默的）。
        """
        logger.info(f"🚀 并行执行子Skill组: {skill_names} | flow_id={flow_id}")

        skill_list = [(self._get_sub_skill(name), name) for name in skill_names]
        skill_list.sort(key=lambda x: x[0].priority)   # 优先级升序：值小者先提交

        results = {}
        failed = []
        with ThreadPoolExecutor(max_workers=len(skill_names)) as executor:
            future_map = {}
            for skill_inst, name in skill_list:
                skill_inst.set_flow_id(flow_id)
                if flow_id:
                    skill_state_manager.update_skill_start(flow_id, name)
                future = executor.submit(skill_inst.run, input_ctx)
                future_map[future] = name

            for future in as_completed(future_map):
                name = future_map[future]
                try:
                    res = future.result()
                    results[name] = res
                    if flow_id:
                        skill_state_manager.update_skill_success(flow_id, name, str(res)[:200])
                    logger.info(f"✅ 子Skill[{name}] 执行成功 | flow_id={flow_id}")
                except Exception as e:
                    logger.error(f"❌ 子Skill[{name}] 执行失败 | flow_id={flow_id} | 错误: {str(e)}")
                    logger.error(traceback.format_exc())
                    if flow_id:
                        skill_state_manager.update_skill_failed(flow_id, name, str(e))
                    # 子Skill失败不阻断整体：置空由主Skill容错处理（失败名回报给调用方）
                    results[name] = {}
                    failed.append(name)
        return results, failed
