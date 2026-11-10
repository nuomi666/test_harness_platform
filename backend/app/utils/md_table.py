# -*- coding: utf-8 -*-
"""
@File    : md_table.py
@Time    : 2026/9/28
@Author  : tanlei

Markdown 表格单元格的**统一转义 / 解析**工具（单一事实源）。

## 解决的问题（P1-13）
用例的「预期结果 / 步骤描述 / 备注」等列里出现 `|` 时（如「使用中|已弃用」、
XPath 的 `//a|//b`、含竖线的元素定位器），`line.split("|")` 会把一个单元格切成两列，
**后续列整体右移**：「备注」被当成「用例等级」；若恰好凑满 9 列，
列数自检还会判为"通过"—— 错误完全无法被发现。

## 约定
- 落盘（_normalize_row / 生成产物）时，单元格内的 `|` 转义为 `\\|`（Markdown 标准转义）；
- 所有解析入口统一走 `split_md_row` 还原；
- 历史数据（未转义的裸 `|`）行为保持不变：仍按 `|` 切分，不会因为本次改动而变化。

## 边界说明
仅处理「反斜杠 + 竖线」这一层转义；`\\\\|`（字面反斜杠后跟分隔符）这类极端写法
不在支持范围，实际用例文本中不会出现。
"""
import re

# 匹配「前面没有反斜杠的竖线」——即真正的列分隔符
_UNESCAPED_PIPE = re.compile(r"(?<!\\)\|")


def escape_md_cell(value) -> str:
    """
    把单元格内容转义为「可安全放进 Markdown 表格」的形式：
    先转义反斜杠本身（避免与 `\\|` 产生歧义），再把 `|` 转义为 `\\|`。
    """
    s = "" if value is None else str(value)
    return s.replace("\\", "\\\\").replace("|", "\\|")


def split_md_row(line: str) -> list:
    """
    按「未转义的 `|`」切分一行 Markdown 表格，并把 `\\|` 还原为 `|`。

    - 首尾的表格边框 `|` 会被剥掉；
    - 末尾的转义竖线 `\\|`（属于单元格内容）不会被误剥。
    """
    s = (line or "").strip()
    if not s:
        return []
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    cells = _UNESCAPED_PIPE.split(s)
    return [c.strip().replace("\\|", "|") for c in cells]
