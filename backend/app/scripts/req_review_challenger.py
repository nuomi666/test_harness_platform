# -*- coding: utf-8 -*-
"""
@File    : req_review_challenger.py
@Time    : 2026/6/10 17:29
@Author  : tanlei
"""
from app.scripts.skill_base import BaseSkill
from typing import Dict, Any


class ReqReviewChallenger(BaseSkill):
    """子Skill：按Checklist挑战需求 + 问题发现"""

    def __init__(self):
        # 优先级1、超时45秒
        super().__init__(
            skill_name="req-review-challenger",
            priority=1,
            timeout=45
        )

    def get_prompt(self, input_context: Dict[str, Any]) -> str:
        requirement = input_context.get("requirement", "")
        checklist = input_context.get("checklist", "")
        history_ctx = input_context.get("history_context", "")
        business_rules = input_context.get("business_rules", "")
        history_cases = input_context.get("history_cases_condensed", "")

        rules_block = ""
        if business_rules and business_rules.strip():
            rules_block = f"""
【该模块/页面已沉淀的业务规则（评审依据）】
{business_rules.strip()}
要求：逐条核对上述规则中的校验点在本需求里是否被交代清楚；规则明确要求、
但需求未提及的，必须作为问题列入问题清单（严重程度按影响判级）。
"""

        cases_block = ""
        if history_cases and history_cases.strip():
            cases_block = f"""
【该模块/页面的历史用例（已精简：仅保留名称/标签/等级/前置/预期摘要）】
{history_cases.strip()}
要求：
1. 若本次需求会改变上述既有用例的预期（例如查询条件被去掉、校验规则被放宽），
   必须列出「受影响用例名称 + 变更点」作为问题或风险项；
2. 也请反查「既有用例已经覆盖、而本次需求未交代」的场景并提示补充；
3. 引用用例时必须逐字照抄清单中的名称，不得编造。
"""

        return f"""
你是专业的需求评审专家，严格遵循以下规则：
1. 基于下方【待评审需求】进行全面评审
2. 严格按照【评审 Checklist】逐项核对
3. 输出包含：问题清单(表格)、评审结论，问题分高/中/低三个等级
{rules_block}{cases_block}
【评审 Checklist】
{checklist if checklist and checklist.strip() else "（无 Checklist）"}

【历史评审经验】
{history_ctx if history_ctx and history_ctx.strip() else "（无历史沉淀）"}

【待评审需求】
{requirement}
"""

    def parse_output(self, raw_response: str) -> Dict[str, Any]:
        return {
            "challenge_result": raw_response,
            "stage": "challenger_completed"
        }