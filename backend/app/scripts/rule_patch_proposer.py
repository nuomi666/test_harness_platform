# -*- coding: utf-8 -*-
"""
@File    : rule_patch_proposer.py
@Time    : 2026/9/15
@Author  : tanlei
"""
import re
import json
import logging
from typing import Dict, Any, List

from app.scripts.skill_base import BaseSkill

logger = logging.getLogger("RULE_PATCH_PROPOSER")


class RulePatchProposer(BaseSkill):
    """
    Skill：规则补丁提案器（方案 C 的生成侧）。

    输入：某页面规则文件全文 + 本次变更确认书（变更描述 + 已确认的疑点答复）
    输出：结构化 JSON 补丁列表，交由人工确认后再由 RulePatchApplier 定点落盘。

    设计底线（写进 prompt，务必保持）：
      - 编号只增不改：新增规则续号（R11/R12），修改只改非编号列，废弃用 deprecate（不物理删）
      - 无依据不提补丁：不得凭想象新增/删改规则
      - 只输出 JSON，便于代码直接消费
    """

    def __init__(self):
        super().__init__(
            skill_name="rule-patch-proposer",
            priority=10,
            timeout=180,
            max_retry=1
        )

    def get_prompt(self, input_context: Dict[str, Any]) -> str:
        return self.get_patch_prompt(
            module_name=input_context.get("biz_module", ""),
            page=input_context.get("page", ""),
            rules_text=input_context.get("rules_text", ""),
            change_desc=input_context.get("change_description", ""),
            clarifications=input_context.get("clarifications", "")
        )

    def parse_output(self, raw_response: str) -> Dict[str, Any]:
        """从模型输出里抽取 JSON（容忍 ```json 代码块与前后解释文字）"""
        data = self._extract_json(raw_response)
        if data is None:
            logger.warning("⚠️ 规则补丁输出不是合法 JSON，已按空补丁处理")
            return {"patches": [], "summary": raw_response[:500], "parse_ok": False}
        patches = data.get("patches") if isinstance(data, dict) else None
        if not isinstance(patches, list):
            patches = []
        # 只保留字段合法的补丁，避免脏数据进入落盘环节
        clean = []
        for p in patches:
            if not isinstance(p, dict):
                continue
            op = str(p.get("op", "")).strip().lower()
            if op not in ("add", "modify", "deprecate"):
                continue
            clean.append({
                "op": op,
                "rule_id": str(p.get("rule_id", "") or "").strip().upper(),
                "table": str(p.get("table", "") or "").strip().upper(),
                "field": str(p.get("field", "") or "").strip(),
                "cells": [str(c) for c in (p.get("cells") or [])] if isinstance(p.get("cells"), list) else [],
                "old": str(p.get("old", "") or ""),
                "new": str(p.get("new", "") or ""),
                "reason": str(p.get("reason", "") or ""),
            })
        logger.info(f"🧾 规则补丁解析完成 | 原始={len(patches)}条 | 合法={len(clean)}条")
        return {
            "patches": clean,
            "summary": str((data or {}).get("summary", "")),
            "parse_ok": True,
        }

    @staticmethod
    def _extract_json(text: str):
        """从可能带代码块/解释文字的输出中提取第一个 JSON 对象"""
        if not text:
            return None
        blocks = re.findall(r"```(?:json)?\s*(.*?)```", text, flags=re.DOTALL)
        candidates = blocks + [text]
        for cand in candidates:
            cand = cand.strip()
            start = cand.find("{")
            end = cand.rfind("}")
            if start == -1 or end == -1 or end <= start:
                continue
            try:
                return json.loads(cand[start:end + 1])
            except Exception:
                continue
        return None

    # ==================== 专用 Prompt ====================
    def get_patch_prompt(
        self,
        module_name: str,
        page: str,
        rules_text: str,
        change_desc: str,
        clarifications: str = ""
    ) -> str:
        clar_block = ""
        if clarifications and clarifications.strip():
            clar_block = f"""
【本次变更已确认的疑点答复】（用户已拍板，必须作为补丁依据）
{clarifications.strip()}
"""
        return f"""你是「业务规则文档维护专家」。请对比【现有规则文件】与【本次变更确认书】，
判断哪些**已有规则**需要修改、哪些**新规则**需要补充、哪些规则**已废弃**，
并输出**结构化补丁 JSON**（不要输出整份规则文件）。

【目标】模块={module_name or "（未指定）"}｜页面={page or "（未指定）"}

【现有规则文件全文】
{rules_text}

【本次变更确认书】
{change_desc}
{clar_block}
【补丁编写铁律】（违反即视为无效补丁）
1. **编号只增不改**：新增规则必须续号（现有最大编号 +1，如 R10 → R11）；
   修改已有规则时**绝对不允许改动编号列**；规则废弃用 deprecate，不得物理删除行。
2. **无依据不提补丁**：只有变更确认书明确涉及、或现有规则明显与之冲突时才提补丁；
   不得凭想象新增规则、不得顺带「优化」无关规则。
3. **modify 只改一个列**：field 必须是该表真实列名之一，new 是替换后的完整单元格文本
   （不要写 diff 语法，直接写改完后的内容）。
4. **deprecate 仅用于规则确实失效**：描述里说明为什么废弃。
5. **表格列结构**（add 的 cells 必须与对应表头对齐，顺序一致）：
   - R 表（核心业务规则）：["规则名", "描述", "等级", "必生成数", "覆盖要求"]
   - S 表（状态流转）：["状态流转", "触发操作", "前置", "等级", "覆盖要求"]
   - F 表（字段约束）：["字段", "控件", "必填条件", "长度/字符集", "说明"]
   （无需在 cells 里写编号，编号由系统按「只增不改」规则自动分配）

【输出格式】**只输出一个 JSON 对象**，不要任何解释文字、不要 Markdown 代码块之外的散文：
```json
{{
  "summary": "一句话说明本次补丁依据",
  "patches": [
    {{"op": "add", "table": "R", "cells": ["规则名", "描述", "P1", "1", "覆盖要求"], "reason": "依据变更第X条"}},
    {{"op": "modify", "rule_id": "R3", "field": "覆盖要求", "old": "改动前的原文（便于人工核对）", "new": "改动后的完整内容", "reason": "依据变更第X条"}},
    {{"op": "deprecate", "rule_id": "R7", "reason": "功能已下线"}}
  ]
}}
```
若本次变更**不需要任何规则调整**，输出 {{"summary": "无需调整", "patches": []}}。
"""


# 全局单例（供 flow_scheduler 复用）
rule_patch_proposer = RulePatchProposer()
