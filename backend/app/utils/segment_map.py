# -*- coding: utf-8 -*-
"""
@File    : segment_map.py
@Time    : 2026/9/30
@Author  : tanlei

产物「分片 → 编号 → 用例行」的机器可读映射（方案 B，单一事实源）。

## 为什么需要它

生成线的编号覆盖自检表曾经**恒为「否 / 0」**：它靠扫用例行的「实质列」找 R/S 编号，
而 `_CHECK_SCAN_COLS` 按设计**排除了备注列**（防止备注随口提一句编号就冒充已覆盖），
可专属规则的编号归属恰恰只写在备注列 ⇒ 永远扫不到。实测产物 14 个编号全是「否 0」，
而这 14 个编号在备注列里各有 2~13 条对应用例。

同时，用例评审（引擎 B）此前按**用例条数**顺序切片（每片 12 条），
与生成线的**规则编号分片**完全不对齐 ⇒ 同一编号的用例被拆到两片，B3（业务正确性）
与 B4（覆盖价值）的打分在不同片里尺度不一致，产生「片间漂移」。

两者是同一个缺口的两个面：**产物里没有记录「哪条用例属于哪个编号」**。

## 载体

产物中追加一行 HTML 注释（紧跟在用例表格之后）：

    <!-- segment_map: {"v":1,"source":"segment","case_count":108,...} -->

注释行不以 `|` 开头 ⇒
- `file_exporter.parse_markdown_table` 视为表格块结束（自检表因此不会被并进用例表）；
- `metadata_manager.parse_markdown_cases` 只认 `|` 开头行 ⇒ 不会混入用例；
- `remove_cases_from_markdown` 只删 `|` 开头行 ⇒ 天然保留；
- 前端 `react-markdown` 不渲染 HTML 注释 ⇒ 用户看不到噪音。

## 归属判定（确定性的，不猜、不伪造）

对每一行用例，按顺序取第一个命中的规则：

1. **备注列严格前缀** `专属规则-R{n}` / `专属规则-S{n}`（模型的实际书写格式）
   —— 这是唯一「同页不同编号片」也能精确定位的依据；
2. **片级归属**：该行来自编号片且该片只负责 1 个编号 ⇒ 归属该编号
   （生成时就是按这个编号生成的，属可靠事实）；
3. **实质列扫描**（旧行为兜底）：来自编号片但负责多编号、或片信息缺失时，
   退回扫非备注列 —— 命中率低，仅作降级，不做主力；
4. 都不命中 ⇒ 记为「归属不明」，**如实回报**而不是硬算成「已覆盖」。
   备注为 `通用规则-{分类}` 或来自通用交互片的行，单独统计为「通用交互用例」，不算缺失。
"""
import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from app.utils.md_table import split_md_row

logger = logging.getLogger("SEGMENT_MAP")

# 映射结构版本（结构变更时递增，旧版本注释行会被忽略而不是误用）
VERSION = 1

# 注释行：`<!-- segment_map: {json} -->`
_MARKER_RE = re.compile(r"<!--\s*segment_map:\s*(\{.*?\})\s*-->", re.DOTALL)

# 备注列的编号归属标注（严格前缀）：「专属规则-R3」「专属规则 S3」「专属规则-R_3」等
_REMARK_OWNED_RE = re.compile(r"专属规则\s*[-－—_:：]\s*([RS]\d+)", re.IGNORECASE)
# 备注列的通用规则标注：「通用规则-查询筛选区」
_REMARK_COMMON_RE = re.compile(r"通用规则\s*[-－—_:：]")
# 规则编号（(?<![A-Za-z0-9]) 防 "SR1"/"AR2" 里的 R1 被误认）
_ANY_ID_RE = re.compile(r"(?<![A-Za-z0-9])([RS])(\d+)")

# 「备注」列在 9 列契约中的下标（对齐 metadata_manager.CASE_HEADERS）
REMARK_COL = 7

# 默认「实质列」扫描范围（与 ReqTestCases._CHECK_SCAN_COLS 一致，排除备注列）
DEFAULT_SCAN_COLS = (0, 1, 2, 3, 4, 5, 6)


# ==================== 注释行读写 ====================

def render_marker(payload: Dict[str, Any]) -> str:
    """把映射结构渲染成一行 HTML 注释（单行、紧凑 JSON，便于人肉 grep）"""
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"<!-- segment_map: {body} -->"


def find_marker(md_text: str) -> Optional[Dict[str, Any]]:
    """解析产物中的 segment_map 注释行；不存在或 JSON 损坏返回 None（多个取最后一个）"""
    if not md_text or "segment_map" not in md_text:
        return None
    matches = _MARKER_RE.findall(md_text)
    if not matches:
        logger.warning("⚠️ 发现 segment_map 字样但无法解析出注释行（格式被改动？）")
        return None
    try:
        payload = json.loads(matches[-1])
    except (ValueError, TypeError) as e:
        logger.warning(f"⚠️ segment_map 注释行 JSON 解析失败，已忽略：{str(e)[:120]}")
        return None
    return payload if isinstance(payload, dict) else None


def load(md_text: str, case_count: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """
    解析并**校验**segment_map。

    ⚠️ 校验用例条数是必需的：产物被人工编辑（增删行、评审删除）后，映射里的行号会失效，
    照用会把「同编号装箱」装错。条数对不上时宁可回退顺序分片，也不要用错的映射。
    """
    payload = find_marker(md_text)
    if not payload:
        return None
    if payload.get("v") != VERSION:
        logger.info(
            f"ℹ️ segment_map 版本不匹配（产物={payload.get('v')}，当前={VERSION}），已忽略"
        )
        return None
    recorded = payload.get("case_count")
    if case_count is not None and isinstance(recorded, int) and recorded != case_count:
        logger.warning(
            f"⚠️ segment_map 已失效：注释记录用例数={recorded}，实际解析={case_count}"
            f"（产物被编辑过？）⇒ 本次评审回退顺序分片"
        )
        return None
    return payload


def strip_marker(md_text: str) -> str:
    """移除产物中的 segment_map 注释行（只删注释行本身，不动表格）"""
    if not md_text or "segment_map" not in md_text:
        return md_text
    kept = [ln for ln in md_text.splitlines() if not _MARKER_RE.search(ln)]
    return "\n".join(kept)


def _find_case_block(lines: List[str]) -> Tuple[int, int]:
    """
    定位「用例表格」的起止行号（0-based，含表头与分隔行）。

    判定：第一段连续的 `|` 开头行中，首行含「用例名称」。找不到返回 (-1, -1)。
    """
    start = -1
    for i, ln in enumerate(lines):
        if ln.strip().startswith("|") and "用例名称" in ln:
            start = i
            break
    if start < 0:
        return (-1, -1)
    end = start
    for j in range(start, len(lines)):
        if lines[j].strip().startswith("|"):
            end = j
        elif lines[j].strip():
            break
    return (start, end)


def upsert_marker(md_text: str, payload: Optional[Dict[str, Any]]) -> str:
    """
    把 segment_map 注释行放到「用例表格最后一行之后」，同时清掉旧注释行。

    只能放在用例表**结束之后**：插在表格块中间会把表格切成两段，
    `parse_markdown_table` 与前端渲染都会错乱。
    payload=None ⇒ 仅移除旧注释行。
    """
    if not md_text:
        return md_text
    lines = md_text.splitlines()
    _, block_end = _find_case_block(lines)
    out: List[str] = []
    inserted = False
    for i, ln in enumerate(lines):
        if _MARKER_RE.search(ln):
            continue                      # 丢弃旧注释行（随后重建）
        out.append(ln)
        if not inserted and payload and i == block_end:
            out.append("")
            out.append(render_marker(payload))
            inserted = True
    if payload and not inserted:
        # 没找到用例表格（异常产物）：退回追加到文末，至少不丢信息
        out.append("")
        out.append(render_marker(payload))
        logger.warning("⚠️ 未定位到用例表格，segment_map 注释行已追加到产物末尾")
    return "\n".join(out)


def collect_case_rows(md_text: str) -> List[str]:
    """取产物中用例表格的数据行（表头/分隔行剔除），用于归属重算"""
    lines = md_text.splitlines()
    start, end = _find_case_block(lines)
    if start < 0:
        return []
    rows = []
    for ln in lines[start:end + 1]:
        s = ln.strip()
        if not s.startswith("|"):
            continue
        if re.fullmatch(r"\|[\s\-:|]+\|", s):
            continue
        if "用例名称" in s:
            continue
        rows.append(s)
    return rows


# ==================== 行 → 编号 归属判定 ====================

def remark_owned_rule_ids(row: str) -> List[str]:
    """从用例行的「备注」列提取 `专属规则-R{n}` 归属编号（严格前缀，不去猜其他写法）"""
    if not row or not row.strip().startswith("|"):
        return []
    cells = split_md_row(row)
    if len(cells) <= REMARK_COL:
        return []
    ids, seen = [], set()
    for m in _REMARK_OWNED_RE.finditer(cells[REMARK_COL]):
        rid = m.group(1).upper()
        if rid not in seen:
            seen.add(rid)
            ids.append(rid)
    return ids


def is_common_row(row: str) -> bool:
    """备注标注为 `通用规则-{分类}` 的用例行（通用交互用例，不参与编号覆盖统计）"""
    if not row or not row.strip().startswith("|"):
        return False
    cells = split_md_row(row)
    if len(cells) <= REMARK_COL:
        return False
    return bool(_REMARK_COMMON_RE.search(cells[REMARK_COL]))


def scan_substantive_rule_ids(row: str, scan_cols=DEFAULT_SCAN_COLS) -> List[str]:
    """扫「实质列」（排除备注列）里的 R/S 编号 —— 降级兜底，命中率低，不作主力"""
    cells = split_md_row(row)
    text = " ".join(cells[i] for i in scan_cols if i < len(cells))
    return [f"{m.group(1)}{m.group(2)}" for m in _ANY_ID_RE.finditer(text)]


def assign_rows(
        rows: List[str],
        row_owner: Optional[List[int]] = None,
        owner_meta: Optional[List[Dict[str, Any]]] = None,
        scan_cols=DEFAULT_SCAN_COLS,
) -> Tuple[Dict[str, List[int]], Dict[str, Any]]:
    """
    逐行判定编号归属。

    :param rows: 用例数据行（顺序 = 产物顺序，行号从 1 开始）
    :param row_owner: 每行所属分片下标（与 owner_meta 对齐，-1/缺失 = 未知）
    :param owner_meta: 每个分片的元信息 [{"type":"common|numbered","rule_ids":[...],"label":...}]
    :return: (rule_rows, stats)
      rule_rows: {编号: [行号, ...]}（**不过滤期望清单** —— 评审装箱需要全量归属；
                 自检表只取期望清单里的编号，过滤发生在渲染时）
      stats: 归属来源统计 + 归属不明的行号，用于如实回报，不做粉饰
    """
    rule_rows: Dict[str, List[int]] = {}
    unassigned: List[int] = []
    common_rows = 0
    by_remark = by_segment = by_scan = 0

    for i, row in enumerate(rows or []):
        no = i + 1
        oi = row_owner[i] if (row_owner and i < len(row_owner)) else -1
        meta = owner_meta[oi] if (owner_meta and 0 <= oi < len(owner_meta)) else None
        seg_type = (meta or {}).get("type") or ""
        seg_ids = [str(x).upper() for x in ((meta or {}).get("rule_ids") or [])]

        ids = remark_owned_rule_ids(row)
        if ids:
            by_remark += 1
        elif is_common_row(row) or seg_type == "common":
            common_rows += 1
            continue
        elif seg_type == "numbered" and len(seg_ids) == 1:
            ids = list(seg_ids)
            by_segment += 1
        elif seg_type == "numbered":
            # 该片负责多个编号且本行没标注归属 ⇒ 无法确定是哪一个。
            # 硬算成「全部编号都命中」会伪造覆盖结论，如实记为归属不明。
            unassigned.append(no)
            continue
        else:
            ids = scan_substantive_rule_ids(row, scan_cols)
            if ids:
                by_scan += 1
            else:
                unassigned.append(no)
                continue

        for rid in ids:
            rule_rows.setdefault(rid, []).append(no)

    stats = {
        "total": len(rows or []),
        "by_remark": by_remark,
        "by_segment": by_segment,
        "by_scan": by_scan,
        "common": common_rows,
        "unassigned": unassigned,
        "enabled": row_owner is not None or owner_meta is not None,
    }
    return rule_rows, stats


def build(
        rows: List[str],
        row_owner: Optional[List[int]] = None,
        owner_meta: Optional[List[Dict[str, Any]]] = None,
        expected_ids: Optional[List[str]] = None,
        source: str = "segment",
        scan_cols=DEFAULT_SCAN_COLS,
) -> Optional[Dict[str, Any]]:
    """
    组装 segment_map 结构；**无任何编号归属可言时返回 None**（宁可不写，也不写空映射）。

    :param source: 映射来源（segment=多片生成 / single_pass=单次全量 / merge=增量定点归并
                   / recompute=增减用例后重算），仅用于排查
    """
    rule_rows, stats = assign_rows(rows, row_owner, owner_meta, scan_cols)
    if not rule_rows and not stats["unassigned"]:
        return None

    payload: Dict[str, Any] = {
        "v": VERSION,
        "source": source,
        "case_count": len(rows or []),
        "rule_rows": {rid: sorted(set(v)) for rid, v in sorted(
            rule_rows.items(), key=lambda kv: (kv[0][0], int(kv[0][1:])))},
    }
    if owner_meta:
        payload["segments"] = [
            {
                "label": (m or {}).get("label") or "",
                "type": (m or {}).get("type") or "",
                "rule_ids": list((m or {}).get("rule_ids") or []),
            }
            for m in owner_meta
        ]
    if stats["unassigned"]:
        payload["unassigned_rows"] = stats["unassigned"]

    logger.info(
        f"🧩 segment_map 生成 | 来源={source} | 用例={stats['total']}条 | "
        f"归属编号={len(rule_rows)}个 | 备注标注={stats['by_remark']} | "
        f"片级归属={stats['by_segment']} | 实质列兜底={stats['by_scan']} | "
        f"通用交互={stats['common']} | 归属不明={len(stats['unassigned'])}"
    )
    return payload


# ==================== 供评审线使用 ====================

def rule_ids_by_row(rule_rows: Optional[Dict[str, List[int]]],
                    case_count: Optional[int] = None) -> Dict[int, List[str]]:
    """把 {编号: [行号]} 反查为 {行号: [编号]}（越界行号丢弃，防脏数据带偏装箱）"""
    mapping: Dict[int, List[str]] = {}
    for rid, rows in (rule_rows or {}).items():
        for r in rows or []:
            try:
                no = int(r)
            except (TypeError, ValueError):
                continue
            if no < 1 or (case_count is not None and no > case_count):
                continue
            mapping.setdefault(no, []).append(str(rid))
    return mapping


def marker_stats(payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """注释行的轻量摘要（供日志/报告使用）"""
    if not payload:
        return {"present": False}
    rule_rows = payload.get("rule_rows") or {}
    return {
        "present": True,
        "source": payload.get("source", ""),
        "case_count": payload.get("case_count"),
        "rule_count": len(rule_rows),
        "unassigned": len(payload.get("unassigned_rows") or []),
    }
