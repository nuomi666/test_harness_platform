# -*- coding: utf-8 -*-
"""
@File    : case_diff_analyzer.py
@Time    : 2026/7/9 12:21
@Author  : tanlei
"""
from app.scripts.skill_base import BaseSkill
from typing import Dict, Any

class CaseDiffAnalyzer(BaseSkill):
    """
    用例差异分析Skill
    职责：需求迭代/版本变更时，对比新旧两版测试用例，自动识别「保留/修改/删除/新增」四类用例
    适用场景：功能模块需求变更、版本迭代、用例回归范围评估
    """
    def __init__(self):
        # 优先级4、超时60秒
        super().__init__(
            skill_name="case-diff-analyzer",
            priority=4,
            timeout=60
        )

    def get_prompt(self, input_context: Dict[str, Any]) -> str:
        old_cases_md = input_context.get("old_cases_md", "")
        new_cases_md = input_context.get("new_cases_md", "")
        change_desc = input_context.get("change_desc", "")

        return f"""
你是资深测试用例变更分析专家，基于【历史版本旧用例】和【新需求生成的临时用例】，输出精准的用例差异分析报告。

===== 输入信息 =====
1. 需求变更说明：
{change_desc}

2. 历史版本旧用例（上一基线版本）：
{old_cases_md}

3. 新需求临时用例（基于变更需求生成）：
{new_cases_md}

===== 分析规则（必须严格执行）=====
1. 逐条对比用例，按以下四类标记每条用例状态：
   - 【有效保留】：功能逻辑、操作步骤、预期结果均无变化，继续沿用
   - 【待修改】：业务规则、字段校验、页面提示/弹窗/报错文案发生变更，需要更新用例内容
   - 【废弃删除】：对应功能下线、场景移除、字段删除，该用例直接剔除
   - 【全新新增】：新需求带来的新功能、新场景、新页面控件，需要补充新增用例

2. 重点关注变更点：
   - 字段规则变更（长度、格式、枚举值）
   - 页面展示变更（提示文案、弹窗内容、按钮状态、样式）
   - 业务流程变更（操作步骤、前置条件、状态流转）
   - 功能下线/新增模块

3. 输出固定结构（Markdown格式）：
### 一、整体变更统计
- 旧版用例总数：X条
- 有效保留：X条
- 待修改：X条
- 废弃删除：X条
- 全新新增：X条

### 二、逐条变更明细
逐条列出，每行一条，格式固定为：
- **用例名称** → 变更类型（有效保留/待修改/废弃删除/全新新增）｜变更说明｜影响范围
（⚠️ 本节**不要用 Markdown 表格**：产物中只允许存在「三」的 9 列用例表格，
  否则导出解析会把本节的小表当成基准表，导致真正的用例表被整表跳过）

### 三、迭代后最终用例（合并版）
直接输出合并后的完整用例表格：保留原有有效用例 + 修改后的用例 + 新增用例，剔除废弃用例。
**表格列必须与主线用例资产使用同一契约（固定 9 列，列名与顺序均不可改）**：
| 用例名称 | 模块 | 标签 | 前置条件 | 步骤描述 | 预期结果 | 编辑模式 | 备注 | 用例等级 |
|---|---|---|---|---|---|---|---|---|

4. 所有分析必须基于输入的新旧用例和变更说明，禁止编造不存在的场景。
5. 废弃用例的名称不再复用；新增用例的名称不得与保留用例重复。
"""

    def parse_output(self, raw_response: str) -> Dict[str, Any]:
        return {
            "diff_report": raw_response,
            "stage": "diff_analyze_completed"
        }
