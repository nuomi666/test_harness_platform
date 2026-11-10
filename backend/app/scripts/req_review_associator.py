# -*- coding: utf-8 -*-
"""
@File    : req_review_associator.py
@Time    : 2026/6/10 17:29
@Author  : tanlei
"""
from app.scripts.skill_base import BaseSkill
from typing import Dict, Any


class ReqReviewAssociator(BaseSkill):
    """子Skill：关联模块识别 + 受影响用例分析"""

    def __init__(self):
        # 自定义：优先级0（最高）、超时45秒
        super().__init__(
            skill_name="req-review-associator",
            priority=0,
            timeout=45
        )

    def get_prompt(self, input_context: Dict[str, Any]) -> str:
        requirement = input_context.get("requirement", "")
        history_ctx = input_context.get("history_context", "")
        business_rules = input_context.get("business_rules", "")
        history_cases = input_context.get("history_cases_condensed", "")

        rules_block = ""
        if business_rules and business_rules.strip():
            rules_block = f"""
【该模块/页面已沉淀的业务规则（评审依据，需逐条核对需求中是否交代清楚）】
{business_rules.strip()}
"""

        cases_block = ""
        if history_cases and history_cases.strip():
            cases_block = f"""
【该模块/页面的历史用例（已精简：仅保留名称/标签/等级/前置/预期摘要，用于判断受影响范围）】
{history_cases.strip()}
要求：「受影响的历史用例」必须**逐字照抄**上方清单中的用例名称并说明受影响原因；
清单中不存在的用例一律不得编造（无受影响用例时输出「无」）。
"""

        return f"""
你是专业的需求关联分析专家，严格遵循以下规则：
1. 仅基于下方【待分析需求】进行关联分析，禁止编造需求中不存在的功能点
2. 输出必须包含：关联模块列表(表格)、受影响历史用例、重点测试范围
3. 信息不足处标注「待确认」，不要臆测
{rules_block}{cases_block}
【历史相似需求的分析经验】
{history_ctx if history_ctx and history_ctx.strip() else "（无历史沉淀）"}

【待分析需求】
{requirement}
"""

    def parse_output(self, raw_response: str) -> Dict[str, Any]:
        return {
            "association_analysis": raw_response,
            "stage": "associator_completed"
        }

    def get_impact_prompt(
        self,
        module_name: str,
        change_desc: str,
        history_cases: str,
        known_clarifications: str = ""
    ) -> str:
        """
        增量模式【影响面分析】专用Prompt：
        分析一次需求变更对现有模块的影响范围，产出供用户确认后再进入增量生成，
        并服务于后续接口/UI回归测试。

        :param known_clarifications: 该 (module, page) 历史已澄清的疑点答复，
                                     注入后要求模型据此自行判断，不再重复提问
        """
        known_block = ""
        if known_clarifications and known_clarifications.strip():
            known_block = f"""
【历史已澄清的业务规则】（同一页面此前已确认过的疑点答复，请直接采信，
**禁止就以下已澄清内容重复提问**；仅当本次变更引入新歧义时才提出新疑点）
{known_clarifications.strip()}
"""
        return f"""
你是资深的需求变更影响面分析专家。请基于【目标模块】【本次变更说明】【历史沉淀用例】，
输出本次变更的影响面分析报告，供测试负责人确认后作为增量用例生成与回归测试的依据。

【目标模块】{module_name or "（未指定）"}

【本次变更说明】
{change_desc}

【历史沉淀用例】（该模块已沉淀的历史用例，用于判断哪些用例会受影响）
{history_cases if history_cases and history_cases.strip() else "（无历史沉淀用例，按全新模块分析）"}
{known_block}
【输出要求】严格按以下四部分输出，缺一不可：

### 一、变更影响范围
- 核心变更点：逐条提炼本次变更的功能点
- 受影响的功能模块：列出受影响的模块及影响说明（高/中/低）
- 受影响的上下游接口：列出可能受影响的接口/数据流（如无则标注「无」）

### 二、受影响的历史用例清单
- 逐条列出历史用例中可能受本次变更影响的用例（编号 + 影响原因），
  无法从历史用例中判断时标注「待确认」，禁止编造不存在的用例编号

### 三、建议回归测试范围
1. 本次新增/修改功能的所有场景
2. 受影响历史用例的回归
3. 关联模块/接口的核心回归

### 四、需用户确认的疑点
若本次分析**确实存在**业务规则不明确处，必须严格输出以下 Markdown 表格
（列名与列顺序固定不可改，一行一个疑点，编号从 Q1 递增，语气为提问）：
| 编号 | 疑点 | 建议默认处理 |
|---|---|---|
| Q1 | （具体的不明确之处） | （你建议的默认做法，供用户直接采纳或修改） |

若本次变更无需用户确认（如已有【历史已澄清的业务规则】可覆盖全部歧义），
则本节仅输出一行：「无」。
**禁止编造歧义**：能依据需求原文与已澄清规则判断的，不要提问。

约束：严格基于给定信息分析，禁止编造不存在的模块、接口或用例。
"""