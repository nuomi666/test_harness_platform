# -*- coding: utf-8 -*-
"""
@File    : case_condenser.py
@Time    : 2026/9/23
@Author  : tanlei

用例精简器：把历史用例基线压成「评审可用的紧凑清单」。

为什么不用大模型做「总结」：
  1. 不需要 —— 结构化基线（metadata_manager）本来就是 9 个字段的字典，压缩是纯规则问题；
  2. 评审链已经有 3 次大模型调用，再加一次就多一份耗时/费用与一个失败面；
  3. 可复现 —— 同样的基线每次都得到同样的清单，便于对比与排查。

三档级别（压缩比基于 bin 模块真实基线实测，原文为 md_content 字数）：
  L1 逐条精简（默认）：用例名称 | 标签 | 用例等级 | 前置条件≤60 | 预期结果≤60
      pool 13391→7556 字（56%）、file_history 10946→6670（61%）、conflict 11371→6311（56%）
  L2 去前置条件、预期压到 40 字：约 45%
  L3 仅定位：用例名称 | 标签 | 用例等级 —— pool 13391→1794 字（13%）

设计取舍：**保留「预期结果」**——业务规则细节（如「卡BIN长度必须6-8位」这类校验点）
大多藏在预期里，全丢会让你在评审时看不到既有的校验依据。
真正省 token 的杀手是丢弃「步骤描述」（占 24~30%）与「前置条件」（占 12~29%）。
"""
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger("CASE_CONDENSER")

# 级别由信息最全到最省 token
LEVELS = ("L1", "L2", "L3")

# 各档保留的字段：(字段名, 截断字数) —— 顺序即输出顺序
# 说明：模块/页面统一提到表头声明（逐行重复「bin/池管理」56 行要白花约 800 字）
_LEVEL_FIELDS = {
    "L1": [("用例名称", None), ("标签", None), ("用例等级", None),
           ("前置条件", 60), ("预期结果", 60)],
    "L2": [("用例名称", None), ("标签", None), ("用例等级", None),
           ("预期结果", 40)],
    "L3": [("用例名称", None), ("标签", None), ("用例等级", None)],
}

_LEVEL_DESC = {
    "L1": "名称 / 标签 / 等级 / 前置条件≤60字 / 预期结果≤60字",
    "L2": "名称 / 标签 / 等级 / 预期结果≤40字",
    "L3": "名称 / 标签 / 等级（仅用于关联定位）",
}

# 兼容模型输出或人工编辑导致的历史列名变体
_FIELD_ALIAS = {
    "用例名称": ("用例名称", "用例名", "用例标题", "标题", "case_name"),
    "标签": ("标签", "标签分类", "tag", "tags"),
    "用例等级": ("用例等级", "等级", "优先级", "priority", "level"),
    "前置条件": ("前置条件", "前置", "前置步骤", "precondition"),
    "预期结果": ("预期结果", "预期", "期望结果", "expected", "expected_result"),
    "模块": ("模块", "所属模块", "module"),
}


def next_level(level: str) -> Optional[str]:
    """返回更低一档（L1→L2→L3→None）；非法级别按 L1 处理"""
    if level not in LEVELS:
        return "L1"
    idx = LEVELS.index(level)
    return LEVELS[idx + 1] if idx + 1 < len(LEVELS) else None


def _pick(case: Dict[str, Any], field: str) -> str:
    """按别名列表取字段值，取不到返回空串"""
    for name in _FIELD_ALIAS.get(field, (field,)):
        val = (case or {}).get(name)
        if val is not None and str(val).strip():
            return str(val)
    return ""


def _clean(text: str) -> str:
    """清理单元格：去掉换行、竖线与 <br> 标记（换行符既破坏表格结构、又纯属噪音）"""
    raw = str(text or "")
    for token in ("<br>", "<br/>", "<br />", "\\n"):
        raw = raw.replace(token, " ")
    # 竖线会破坏 markdown 表格结构，统一换成斜杠
    return " ".join(raw.replace("|", "/").split())


def _clip(text: str, limit: Optional[int]) -> str:
    text = str(text or "").strip()
    if limit and len(text) > limit:
        return text[:limit] + "…"
    return text


def condense_cases(
        cases: List[Dict[str, Any]],
        level: str = "L1",
        scope_label: str = "",
        version: str = ""
) -> Dict[str, Any]:
    """
    把结构化用例列表压成紧凑清单文本。

    :param cases: 结构化用例列表（metadata_manager 的 case_list，键为 9 列表头名）
    :param level: 精简级别 L1/L2/L3（非法值按 L1）
    :param scope_label: 归属描述，如「bin / 池管理」，写入清单表头
    :param version: 基线版本号，写入清单表头（便于对账）
    :return: {"text", "level", "count", "chars", "fields"}
    """
    level = level if level in LEVELS else "L1"
    fields = _LEVEL_FIELDS[level]
    case_list = list(cases or [])

    lines = []
    for case in case_list:
        vals = [_clip(_clean(_pick(case, f)), lim) or "-" for f, lim in fields]
        lines.append(" | ".join(vals))

    # 标签分布概览：让模型先看全局结构，再逐条判断相关性（约 60~120 字，性价比高）
    tag_count: Dict[str, int] = {}
    for case in case_list:
        tag = _clean(_pick(case, "标签")) or "未分类"
        tag_count[tag] = tag_count.get(tag, 0) + 1
    tag_line = "、".join(
        f"{k}({v})" for k, v in sorted(tag_count.items(), key=lambda x: -x[1])[:12]
    )

    header = (
        f"【历史用例清单（已精简，级别 {level}：{_LEVEL_DESC[level]}；"
        f"每条用例的完整「步骤描述」与「备注」未注入）】\n"
        f"范围：{scope_label or '未指定'}"
        + (f" | 基线版本：{version}" if version else "")
        + f" | 共 {len(case_list)} 条\n"
        + (f"标签分布：{tag_line}\n" if tag_line else "")
    )
    text = (
        header
        + "\n".join(lines)
        + "\n（以上为精简清单，用于判断既有覆盖情况与受影响范围；需要完整步骤时请查看用例基线产物）"
    )

    logger.info(
        f"🗜️ 历史用例精简完成 | 级别={level} | 条目={len(case_list)} | "
        f"输出={len(text)}字 | 字段={'/'.join(f for f, _ in fields)}"
    )
    return {
        "text": text,
        "level": level,
        "count": len(case_list),
        "chars": len(text),
        "fields": [f for f, _ in fields],
    }


def cases_from_markdown(md_content: str) -> List[Dict[str, Any]]:
    """
    兜底通道：把整篇基线用例 markdown 解析回结构化条目。

    使用场景：结构化元数据取不到（旧数据、只沉淀过向量库）时，
    从向量召回拿到的是整篇 markdown，需先解析成条目才能精简。
    """
    try:
        from app.utils.metadata_manager import meta_manager
        return meta_manager.parse_markdown_cases(md_content or "")
    except Exception as e:
        logger.warning(f"⚠️ 从 markdown 解析历史用例失败: {str(e)}")
        return []
