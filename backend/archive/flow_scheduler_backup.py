# -*- coding: utf-8 -*-
"""
@File    : flow_scheduler.py
@Time    : 2026/6/10 15:03
@Author  : tanlei
"""
import os
import time
from typing import Optional

"""Harness流程调度器（带沉淀复用版）"""
from app.core.vector_db import ChromaDBSingleton
from app.services.llm_adapter import LLMAdapter
from app.utils.context_manager import context_manager
from app.utils.file_output_manager import output_manager
from app.core.config import harness_core_config

class FlowScheduler:
    def __init__(self):
        self.llm = LLMAdapter()
        self.vector_db = ChromaDBSingleton.get_instance()

    def _retrieve_full_context(self, query: str, skill_filter: str = None):
        """
        全量上下文检索：
        1. 通用技能库
        2. 历史相似需求的沉淀资产
        3. 按Skill类型过滤（可选）
        """
        res = self.vector_db.search_all(query, n_results=20)
        biz_ctx = ""
        skill_ctx = ""
        history_ctx = ""

        for doc, meta in zip(res["documents"][0], res["metadatas"][0]):
            if meta["type"] == "business":
                biz_ctx += doc + "\n"
            elif meta["type"] == "skill":
                if skill_filter is None or meta["skill_name"] == skill_filter:
                    skill_ctx += doc + "\n"
            elif meta["type"] == "project_asset":
                # 历史沉淀资产，单独拼接
                history_ctx += f"\n【历史相似{meta['asset_type']}】：\n{doc}\n"

        context_manager.set_rag_context(biz_ctx, skill_ctx)
        return biz_ctx, skill_ctx, history_ctx

    def run_requirement_review(self, requirement: str) -> dict:
        """执行第一阶段：需求评审（带历史沉淀复用）"""
        slug = output_manager.generate_slug(requirement[:50])
        context_manager.start_flow(requirement, slug)
        context_manager._context["current_stage"] = "reviewing"

        # 检索全量上下文：通用评审技能 + 历史相似评审报告
        biz_ctx, skill_ctx, history_ctx = self._retrieve_full_context(
            requirement, skill_filter="req-review"
        )

        # 组装Prompt，加入历史沉淀内容
        prompt = f"""
        你是遵循Harness契约的专业测试需求评审专家，严格遵守以下规则：
        1. 业务需求依据：{biz_ctx}
        2. 评审技能与Checklist：{skill_ctx}
        3. 【重要】参考历史相似需求的评审经验：{history_ctx}
        4. 输出标准Markdown格式评审报告，包含：需求概述、问题列表、关联模块、改进建议、评审结论
        5. 禁止编造问题，所有问题必须基于需求原文或历史经验

        待评审需求：{requirement}
        """

        try:
            report = self.llm.generate(prompt)
            context_manager.set_review_report(report)
            # 保存并自动沉淀评审报告
            output_manager.save_and_precipitate(slug, "review_report", report, requirement)
            return {
                "slug": slug,
                "review_report": report,
                "stage": "reviewed",
                "history_used": len(history_ctx) > 0
            }
        except Exception as e:
            raise Exception(f"需求评审失败: {str(e)}")

    def run_review_generation(
            self,
            biz_module: str,
            requirement_desc: str,
            upload_file_bytes: Optional[bytes] = None,
            upload_filename: str = ""
    ) -> dict:
        """
        需求评审报告生成【py3.9兼容，基于SKILL.md编排的req-review Skill】
        :param biz_module: 前端选择的业务模块，存入flow上下文
        :param requirement_desc: 需求描述文本（必填）
        :param upload_file_bytes: 上传文件bytes，None=不上传文件
        :param upload_filename: 上传文件名称
        """
        # 1.解析上传文档
        doc_content = ""
        if upload_file_bytes and upload_filename:
            from app.utils.file_parser import extract_document_text
            doc_content = extract_document_text(upload_file_bytes, upload_filename)

        # 2.查询同模块历史评审沉淀资产
        history_reports_text = ""
        if biz_module and biz_module.strip():
            history_docs = self.vector_db.search_by_module(
                query=f"需求评审 {requirement_desc}",
                module_name=biz_module.strip(),
                n_results=3,
                asset_type="review_report"
            )
            history_reports_text = "\n\n".join([d["document"] for d in history_docs])

        # 3.组装输入上下文，传给req-review skill（会自动调度associator + challenger子skill）
        skill_input = {
            "requirement_text": requirement_desc.strip(),
            "doc_content": doc_content,
            "history_reports": history_reports_text,
            "biz_module": biz_module.strip()
        }
        # 调用现有skill引擎执行req-review（核心改动，不再手写prompt）
        report_content = self.skill_engine.run_skill(skill_name="req-review", input_data=skill_input)

        # 4.写入flow上下文，沉淀接口读取biz_module
        slug = f"review_{biz_module}_{int(time.time())}"
        flow_id = f"flow_{slug}"
        self.context_manager.init_context(flow_id, slug)
        self.context_manager.update_context(flow_id, {
            "slug": slug,
            "biz_module": biz_module.strip(),
            "review_report": report_content,
            "requirement_text": requirement_desc.strip(),
            "upload_doc_text": doc_content
        })

        return {
            "code": 0,
            "flow_id": flow_id,
            "slug": slug,
            "review_report": report_content,
            "biz_module": biz_module.strip(),
            "has_history": bool(history_reports_text),
            "has_upload_doc": bool(doc_content)
        }
    def run_test_case_generation(self, slug: str) -> dict:
        """执行第二阶段：生成测试用例（带历史沉淀复用）"""
        ctx = context_manager.get_context()
        if ctx["current_stage"] != "reviewed":
            raise Exception("请先完成需求评审")

        context_manager._context["current_stage"] = "generating"

        # 检索全量上下文：通用用例生成技能 + 历史相似用例
        biz_ctx, skill_ctx, history_ctx = self._retrieve_full_context(
            ctx["requirement_text"], skill_filter="req-test-cases"
        )

        supplement_text = ""
        if ctx["supplement_rules"]:
            supplement_text = "\n【补充业务约束】：" + "；".join(ctx["supplement_rules"])

        # 组装Prompt，加入历史沉淀内容
        prompt = harness_core_config["harness"]["llm"]["prompt_template"].format(
            business_context=f"{biz_ctx}\n\n需求评审报告：{ctx['review_report']}\n\n【历史相似用例参考】：{history_ctx}",
            skill_context=skill_ctx,
            user_query=f"{ctx['requirement_text']}{supplement_text}"
        )

        try:
            print(f"调用的文件是: flow_scheduler")
            cases = self.llm.generate(prompt)
            context_manager.set_test_cases(cases)
            # 保存并自动沉淀测试用例
            output_manager.save_and_precipitate(slug, "test_cases", cases, ctx["requirement_text"])
            return {
                "slug": slug,
                "test_cases": cases,
                "stage": "completed",
                "history_used": len(history_ctx) > 0
            }
        except Exception as e:
            raise Exception(f"测试用例生成失败: {str(e)}")

    def promote_to_general_skill(self, slug: str, asset_type: str, skill_name: str):
        """
        手动沉淀：将优质项目资产升级为通用团队技能
        :param slug: 流程ID
        :param asset_type: review_report / test_cases
        :param skill_name: 要加入的技能名称（如req-review）
        """
        ctx = context_manager.get_context()
        if not ctx["slug"]:
            raise Exception("流程不存在")

        content = ctx["review_report"] if asset_type == "review_report" else ctx["test_cases"]

        # 保存到通用技能库的references目录
        skill_dir = f"./app/skills/{skill_name}/references"
        os.makedirs(skill_dir, exist_ok=True)
        filename = f"precipitated_{asset_type}_{slug}.md"
        file_path = os.path.join(skill_dir, filename)

        with open(file_path, "w", encoding="utf-8") as f:
            f.write(f"# 沉淀自流程：{slug}\n## 原始需求：{ctx['requirement_text']}\n\n{content}")

        # 重新加载所有技能，使其立即生效
        from app.services.plugin_loader import load_all_skills
        load_all_skills()

        return {"message": f"已成功沉淀为通用技能：{filename}"}