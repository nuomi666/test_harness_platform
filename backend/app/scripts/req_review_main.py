# -*- coding: utf-8 -*-
"""
@File    : req_review_main.py
@Time    : 2026/6/10 17:29
@Author  : tanlei
"""
from app.scripts.skill_base import BaseSkill
from typing import Dict, Any


class ReqReviewMain(BaseSkill):
    """主Skill：汇总子Skill输出，生成最终评审报告"""

    def __init__(self):
        # 优先级2、超时60秒
        super().__init__(
            skill_name="req-review-main",
            priority=2,
            timeout=120
        )

    def get_prompt(self, input_context: Dict[str, Any]) -> str:
        requirement = input_context.get("requirement", "")
        association_result = input_context.get("association_result", "")
        challenge_result = input_context.get("challenge_result", "")
        business_rules = input_context.get("business_rules", "")

        rules_block = ""
        if business_rules and business_rules.strip():
            rules_block = f"""
4. 业务规则依据（该模块/页面已沉淀规则）：
{business_rules.strip()}
汇总要求：报告中「评审发现的问题」需覆盖「需求与已沉淀业务规则冲突或遗漏」的项。
"""

        return f"""
你是测试评审报告撰写专家，基于以下内容生成标准Markdown评审报告：
1. 原始需求：{requirement}
2. 关联分析结果：{association_result}
3. 需求问题评审结果：{challenge_result}
{rules_block}
报告必须包含：需求概述、关联模块、问题清单、改进建议、最终评审结论。
"""

    def parse_output(self, raw_response: str) -> Dict[str, Any]:
        return {
            "final_review_report": raw_response,
            "stage": "main_review_completed"
        }