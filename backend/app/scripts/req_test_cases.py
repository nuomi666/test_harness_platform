# -*- coding: utf-8 -*-
"""
@File    : req_test_cases.py
@Time    : 2026/6/10 17:29
@Author  : tanlei
"""
import logging
import os
import re
from app.scripts.skill_base import BaseSkill
from app.services.llm_adapter import LLMTruncationError
from app.utils.md_table import escape_md_cell, split_md_row
from app.utils import segment_map
from typing import Dict, Any

logger = logging.getLogger("SKILL_REQ_TEST_CASES")

# 统一 9 列表头（分片合并时由代码统一输出，保证与 file_exporter.CASE_HEADERS 一致）
CASE_HEADER = "| 用例名称 | 模块 | 标签 | 前置条件 | 步骤描述 | 预期结果 | 编辑模式 | 备注 | 用例等级 |"
CASE_SEPARATOR = "|---|---|---|---|---|---|---|---|---|"


class ReqTestCases(BaseSkill):
    """测试用例生成Skill（支持编号分片 + 编号覆盖自检）"""

    # 每片最多承载的规则编号数（R/S），超出则分片；可用环境变量覆盖
    SEGMENT_SIZE = int(os.getenv("LLM_SEGMENT_SIZE", "6"))

    def __init__(self):
        # 优先级3、超时180秒（用例生成耗时更长）
        super().__init__(
            skill_name="req-test-cases",
            priority=3,
            timeout=180
        )

    # ==================== 编号分片相关 ====================

    def _extract_rule_ids(self, module_rules: str):
        """
        从模块规则中提取 R/S 编号，作为分片与逐条核对的唯一单位。
        F 表（字段约束）是字段校验用例的事实依据，全文随每片注入，不单独分片。
        返回去重后按 (字母, 数字) 升序排序的编号列表，如 ["R1","R2","R10","S1"]。
        —— 必须排序：规则文档正文常提前引用某编号（如「见 R11」），若按出现顺序
        会导致分片把不相关规则混在一片、合并后的自检表顺序混乱。
        """
        if not module_rules or not module_rules.strip():
            return []
        ids = []
        seen = set()
        for m in re.finditer(r"\b([RS])(\d+)\b", module_rules):
            full = f"{m.group(1)}{m.group(2)}"
            if full not in seen:
                seen.add(full)
                ids.append(full)
        # 按 (字母, 数字) 排序：保证分片与「编号覆盖自检表」顺序稳定、语义连贯
        ids.sort(key=lambda x: (x[0], int(x[1:])))
        return ids

    @staticmethod
    def _chunk(ids, size):
        return [ids[i:i + size] for i in range(0, len(ids), size)]

    def _merge_segments(self, segment_outputs, expected_rule_ids=None, failed_segments=None,
                        segments_meta=None):
        """
        合并多片输出为「单一 9 列表格 + 单一张代码级编号覆盖自检表」。

        - 用例表头由代码统一输出（CASE_HEADER），不依赖模型各自输出的表头；
        - **每条用例行都过 `_normalize_row` 归一化为 9 列**（原来是把模型原始行直接追加，
          列数不对也照样落盘、且无任何告警，9 列契约在生成线形同虚设）；
        - 模型各自输出的自检表行一律丢弃（不可靠）；末尾自检表由代码扫描合并后的用例行统计生成；
        - 合并后做一次列数自检（与增量线 `merge_full_cases` 同一口径），异常直接告警；
        - **输出一行机器可读的 segment_map 注释（方案 B）**：记录「每片负责哪些编号 +
          每行归属哪个编号」。自检表的「是否覆盖/命中条数」直接读它（不再靠扫列猜，
          扫列方案因排除备注列而恒为「否/0」）；评审线的引擎 B 也读它做「按编号装箱」。

        :param failed_segments: 未产出的分片标签；非空时在产物末尾如实标注缺了哪几片。
        :param segments_meta: 与 segment_outputs 一一对应的分片元信息
                              [{"label":.., "type":"common|numbered", "rule_ids":[..]}]
        """
        case_rows = []   # 用例数据行（已归一化为 9 列）
        row_owner = []   # 每行所属分片下标（与 segments_meta 对齐）
        raw_dist = {}    # 模型「原始列数」分布（归一化前统计，否则列数异常永远看不到）

        for seg_idx, out in enumerate(segment_outputs or []):
            if not out or not out.strip():
                continue
            for line in out.strip().split("\n"):
                s = line.strip()
                if not s.startswith("|"):
                    continue
                # 模型编号自检表数据行：| R1 | 是 | xxx |（不可靠，丢弃）
                if re.match(r"^\|\s*[RS]\d+\s*\|", s):
                    continue
                # 表头行（含「用例名称」）与分隔行（|---|）跳过，统一由代码输出
                if "用例名称" in s:
                    continue
                if re.match(r"^\|[\s\-:|]+\|$", s):
                    continue
                # 通用规则覆盖自检表相关行（3 列、第二列为「是否覆盖/是/否」）跳过，不并入用例表
                cells = split_md_row(s)
                if len(cells) == 3 and (
                    "覆盖" in cells[1] or cells[1].strip() in ("是", "否", "是/否")
                ):
                    continue
                if not self._is_case_like_row(s):
                    logger.warning(f"⚠️ 忽略非用例形态的表格行（列数={len(cells)}）：{s[:80]}")
                    continue
                # 归一化**之前**如实统计原始列数：归一化后列数必然为 9，
                # 若不做这一步，模型少列/多列（单元格含裸 `|`）就彻底看不到了
                raw_dist[len(cells)] = raw_dist.get(len(cells), 0) + 1
                # 用例数据行：统一归一化为 9 列（列数不对不再静默落盘）
                case_rows.append(self._normalize_row(s))
                row_owner.append(seg_idx)

        odd_raw = {k: v for k, v in raw_dist.items() if k != 9}
        if odd_raw:
            logger.warning(
                f"⚠️ 模型输出的用例行存在非 9 列（已按 9 列归一，但内容可能已错位，请人工核对）| "
                f"原始列数分布={odd_raw}（常见原因：单元格内含裸 `|` 未转义）"
            )

        # 合并后列数自检（与增量线同一口径）
        dist = {}
        for row in case_rows:
            n = len(split_md_row(row))
            dist[n] = dist.get(n, 0) + 1
        bad = {k: v for k, v in dist.items() if k != 9}
        if bad:
            logger.warning(
                f"⚠️ 分片合并列数自检异常（预期每行 9 列）| 异常分布={bad} | 请人工核对产物"
            )

        if not case_rows:
            # 不留「空表头 + 全否自检表」：那种产物会被 file_exporter 把 3 列自检表
            # 当成第一个表格，最终导出一份 3 列垃圾。返回空串，由调用方按失败处理。
            logger.error("❌ 分片合并后未得到任何用例行（模型未产出用例表格）")
            return ""

        parts = [CASE_HEADER, CASE_SEPARATOR] + case_rows

        # 机器可读分片映射（方案 B）：编号覆盖自检表与评审「按编号装箱」的唯一依据。
        # 位置固定在用例表之后：注释行不以 `|` 开头 ⇒ 表格解析、用例解析、导出都不受影响。
        payload = segment_map.build(
            case_rows, row_owner=row_owner, owner_meta=segments_meta,
            expected_ids=list(expected_rule_ids or []), source="segment",
        )
        if payload:
            parts.append("")
            parts.append(segment_map.render_marker(payload))
        else:
            logger.warning(
                "⚠️ 未能建立编号归属映射（用例行既无备注标注、也无分片归属）"
                "⇒ 自检表退回扫实质列判定，命中率极低（历史缺陷：恒为「否/0」）"
            )

        # 代码级编号覆盖自检表（不信任模型自报，按上面的归属映射统计）
        check_part = self._build_code_check_table(
            case_rows, list(expected_rule_ids or []), payload=payload
        )
        if check_part:
            parts.append(check_part)

        if failed_segments:
            parts.append("")
            parts.append(
                "> ⚠️ 以下分片未产出（相应编号的用例缺失，请人工补漏或重跑）："
                + "、".join(failed_segments)
            )

        merged = "\n".join(parts)
        logger.info(
            f"🧩 分片合并完成 | 片数={len(segment_outputs or [])} | 用例={len(case_rows)}条 | "
            f"列数分布={dist} | 未产出片={failed_segments or '无'} | 长度={len(merged)}字"
        )
        return merged

    # 自检命中判定所依据的列（0-based）：用例名称/模块/标签/前置条件/步骤描述/预期结果/编辑模式
    # **明确排除「备注」列（第 8 列）**：prompt 要求模型在备注里写「按 R3 校验」，
    # 若照原实现扫整行，则只要备注提了编号就计为命中，与是否真生成了对应用例无关，
    # 自检表会被备注轻易伪造（误报「已覆盖」，漏生成被掩盖）。
    _CHECK_SCAN_COLS = (0, 1, 2, 3, 4, 5, 6)

    def _scan_row_rule_ids(self, row: str) -> list:
        """从一行的**实质列**中提取 R/S 编号（排除「备注」列，见 `_CHECK_SCAN_COLS`）

        ⚠️ 这只是**降级兜底**：专属规则的编号归属写在备注列，而备注列被排除
        ⇒ 本方法在真实产物上几乎恒为空。归属判定请走 `segment_map`（方案 B）。
        """
        return segment_map.scan_substantive_rule_ids(row, self._CHECK_SCAN_COLS)

    def _build_code_check_table(self, case_rows, expected_ids, payload=None):
        """
        纯代码生成「编号覆盖自检表」：如实反映每项编号是否真的有对应用例。

        命中判定的两条路径（方案 B 之后以第一条为主）：
        1. `payload`（segment_map）非空 → 命中条数 = 该编号归属到的用例行数。
           归属来自「备注列 `专属规则-R{n}` 严格前缀 / 单编号片级归属 / 实质列兜底」，
           由 `segment_map.assign_rows` 确定性判定，并统计「归属不明」条数如实回报；
        2. `payload` 为空（老产物 / 注释行被剥离）→ 退回扫实质列（旧行为，命中率低）。

        :param payload: `segment_map.build()` 的返回值；None 表示无归属映射可用
        """
        if not expected_ids:
            return ""
        unassigned, attribution_note = [], None
        if payload:
            rule_rows = payload.get("rule_rows") or {}
            hit = {rid: len(rows) for rid, rows in rule_rows.items() if rid in expected_ids}
            unassigned = list(payload.get("unassigned_rows") or [])
            attribution_note = (
                f"> 归属依据：备注列标注 / 分片编号（共 {payload.get('case_count', len(case_rows))} 条用例，"
                f"归属到 {len(rule_rows)} 个编号）。"
            )
        else:
            # 降级路径：扫实质列（排除备注列）—— 真实产物上几乎扫不到编号
            hit = {}
            for row in case_rows:
                for full in self._scan_row_rule_ids(row):
                    if full in expected_ids:
                        hit[full] = hit.get(full, 0) + 1
            attribution_note = (
                "> ⚠️ 本产物无编号归属映射（segment_map 缺失），自检退回「扫实质列」判定 —— "
                "该方法**排除备注列**，而专属规则的编号归属写在备注列 ⇒ 命中数普遍偏低甚至为 0，"
                "请以备注列人工核对为准。"
            )

        lines = [
            "",
            "## 编号覆盖自检表（代码核对，非模型自报）",
            "| 规则编号 | 是否覆盖 | 命中条数 |",
            "|---|---|---|",
        ]
        missing = []
        for rid in expected_ids:
            if rid in hit:
                lines.append(f"| {rid} | 是 | {hit[rid]} |")
            else:
                lines.append(f"| {rid} | 否 | 0 |")
                missing.append(rid)

        # 汇总：如实列出缺失编号，供人工补漏
        lines.append("")
        total = len(expected_ids)
        hitn = len(hit)
        missn = total - hitn
        lines.append(f"> 共 {total} 个编号，命中 {hitn} 个，缺失 {missn} 个。")
        lines.append(attribution_note)
        if unassigned:
            lines.append(
                f"> ⚠️ 另有 {len(unassigned)} 条用例未能归属到任何编号"
                f"（备注未按 `专属规则-R{{n}}` 标注，且所在分片负责多个编号）"
                f"—— 这些用例的覆盖情况**未计入上表**，请人工核对。"
            )
        if missing:
            lines.append(f"> ⚠️ 以下编号未生成对应用例，请人工补漏：**{', '.join(missing)}**")
        else:
            lines.append("> ✅ 全部编号均已生成对应用例。")
        return "\n".join(lines)

    # ==================== 增量定点归并（不重写全量，消除输出截断）====================
    # 背景：原实现把「历史全量 + 增量清单」交给大模型，要求输出合并后的**完整**新表格。
    #       该输出是单次调用，必然撞 max_tokens 上限 —— 实测池页基线（56 条）单独就占
    #       8192 上限的 98%，合并后 60 条 ≈ 8643 tokens，结果：末行被截断（缺后 3 列）、
    #       4 条新增只落 2 条、代码级编号自检表整个丢失。
    # 现改为「定点归并」—— 合并本身是机械操作，不需要重写全文：
    #   新增 → 代码直接追加；删除 → 按用例名剔除；修改 → 按用例名定点替换；
    #   仅当「修改项写的被改对象名与基线对不上」时，才请大模型做一次定位（输入只有名称清单）。
    # 结果：输出量与「变更条数」成正比、与用例总数无关，用例涨到几百条也不会截断；
    #       未变更用例逐字保持原样，不会被模型顺手润色漂移。

    # 增量清单章节标题关键词（容错匹配，模型可能写成「新增的用例」「修改点」等）
    _SEC_ADD = ("新增", "增加")
    _SEC_MOD = ("修改", "变更")
    _SEC_DEL = ("删除", "废弃", "下线")

    @staticmethod
    def _norm_case_name(name: str) -> str:
        """用例名归一化（去空白、全角标点转半角、去装饰符），用于宽松匹配同名用例"""
        if not name:
            return ""
        s = re.sub(r"[\s\u3000]+", "", str(name).strip())
        for a, b in (("（", "("), ("）", ")"), ("：", ":"), ("，", ","), ("、", ","),
                     ("「", ""), ("」", ""), ("《", ""), ("》", ""),
                     ("**", ""), ("`", ""), ("*", ""), ("【", ""), ("】", "")):
            s = s.replace(a, b)
        return s.lower()

    @staticmethod
    def _case_name(row: str) -> str:
        """取一行用例的「用例名称」列（第 1 列）"""
        if not row:
            return ""
        cells = split_md_row(row)
        return cells[0].strip().replace("**", "").strip() if cells else ""

    @staticmethod
    def _is_self_check_row(line: str) -> bool:
        """
        判定是否是「编号覆盖自检表」的行（表头/分隔/数据），归并时必须剔除。
        自检表列数不固定：模型写过 3 列（规则编号|是否覆盖|命中条数），
        也写过 5 列（…|是否覆盖（增量补充前）|命中条数（增量补充前）|是否覆盖（增量补充后）|…），
        故不能按固定列数判断 —— 用例行固定 9 列，凡是「列数 < 9 且含『覆盖』」一律视为自检表。
        """
        s = (line or "").strip()
        if not s.startswith("|"):
            return False
        if "用例名称" in s:
            return True
        if re.match(r"^\|[\s\-:|]+$", s):
            return True
        if re.match(r"^\|\s*[RS]\d+\s*\|", s):            # | R11 | 是 | 4 |
            return True
        cells = split_md_row(s)
        if cells and cells[0] in ("规则编号", "编号", "通用规则分类"):
            return True
        if len(cells) < 9 and any("覆盖" in c for c in cells):
            return True
        return False

    @staticmethod
    def _normalize_row(row: str) -> str:
        """
        把一行规整为标准 9 列：不足补空、超出并入末列（保证产物列数一致，方便导出与解析）。

        两侧都做处理，避免「列错位」这个静默错误：
        - 解析侧：走 split_md_row，单元格内的 `\\|` 不会被当成列分隔符；
        - 输出侧：用 escape_md_cell 把单元格内的 `|` 转义，否则下次再解析又会错位。
        """
        cells = split_md_row(row)
        if len(cells) > 9:
            # 超出列数：并入末列而不是直接丢弃 —— 丢内容属静默损坏，比多一列更糟
            cells = cells[:8] + [" ".join(cells[8:])]
        while len(cells) < 9:
            cells.append("")
        return "| " + " | ".join(escape_md_cell(c) for c in cells) + " |"

    @staticmethod
    def _is_case_like_row(row: str) -> bool:
        """粗筛「像用例行」的行：至少 5 列，避免把说明性小表格混进产物"""
        return len(split_md_row(row)) >= 5

    def _parse_case_rows(self, text: str) -> list:
        """提取用例数据行（剔除表头/分隔行/自检表行），保持原顺序"""
        rows = []
        for line in (text or "").split("\n"):
            s = line.strip()
            if s.startswith("|") and not self._is_self_check_row(s):
                rows.append(s)
        return rows

    def _split_incremental_sections(self, text: str) -> Dict[str, Any]:
        """
        把增量清单切成三节。返回 {"add": [行], "modify": [行], "delete": [原始行], "title_hits": n}

        容错点：
        - 「## 编号覆盖自检表（仅补充本次新增的 R11…）」这类标题**含「新增」但不是章节标题**，
          必须先按「自检/覆盖」排除，否则会把自检表误当新增用例收进产物；
        - 识别不到任何章节标题时 title_hits=0，由调用方走「按用例名归类」的宽松策略。
        """
        result = {"add": [], "modify": [], "delete": [], "loose": [], "title_hits": 0}
        cur = None
        for line in (text or "").split("\n"):
            s = line.strip()
            if s.startswith("#"):
                head = s.lstrip("#").strip()
                if "自检" in head or "覆盖" in head:
                    cur = None
                    continue
                if any(k in head for k in self._SEC_DEL):
                    cur = "delete"
                elif any(k in head for k in self._SEC_MOD):
                    cur = "modify"
                elif any(k in head for k in self._SEC_ADD):
                    cur = "add"
                else:
                    cur = None
                if cur:
                    result["title_hits"] += 1
                continue

            if not s or s.startswith(">"):
                continue
            if cur == "delete":
                # 删除节：表格行与纯文本行都要留（模型常写成「- 用例名（原因）」）
                result["delete"].append(s)
            elif s.startswith("|"):
                if self._is_self_check_row(s):
                    continue
                # 未落在任何已识别章节里的表格行 → 单独放 loose，
                # 不默认当「新增」（否则自检表、统计表会被混进产物）
                result["add" if cur == "add" else ("modify" if cur == "modify" else "loose")].append(s)

        return result

    def _extract_deleted_items(self, delete_lines, baseline_rows) -> list:
        """
        从「建议删除用例」节提取待删项，兼容三种写法：
        ① 表格行（第 1 列 = 用例名，其余列拼成原因）；
        ② 加粗 / 项目符号文本；③ 纯文本中提到了基线用例名。

        返回 [{"name": 用例名, "reason": 删除原因, "matched": 是否命中基线, "raw": 原始行}]。
        matched 用于前端提示「模型写了一个基线里并不存在的用例名」——
        这类项若被直接执行删除是无效动作，人工确认时应当能看出来。
        """
        norm_index = {}
        for row in baseline_rows:
            key = self._norm_case_name(self._case_name(row))
            if key:
                norm_index.setdefault(key, self._case_name(row))

        items, seen = [], set()
        for raw in delete_lines or []:
            s = raw.strip().lstrip("-*•0123456789.、) ").strip()
            if not s or s in ("无", "无。", "—", "-"):
                continue
            cand, reason = None, ""
            if s.startswith("|"):
                cells = split_md_row(s)
                cand = cells[0] if cells else None
                reason = "；".join(
                    c for c in cells[1:] if c and not re.match(r"^[\s\-:]+$", c)
                )
            else:
                s2 = self._norm_case_name(s)
                for key, orig in norm_index.items():
                    if key and key in s2:
                        cand, reason = orig, s
                        break
                if cand is None and len(s) <= 60:
                    cand = s            # 短文本按「本身就是用例名」处理
            if not cand:
                continue
            cand = cand.replace("**", "").strip()
            if not cand or cand in ("无", "用例名称", "原因", "删除原因"):
                continue
            key = self._norm_case_name(cand)
            if key in seen:
                continue
            seen.add(key)
            items.append({
                "name": cand,
                "reason": reason,
                "matched": key in norm_index,
                "raw": raw.strip()[:200],
            })
        return items

    def _extract_deleted_names(self, delete_lines, baseline_rows) -> list:
        """兼容旧调用：只取用例名列表"""
        return [it["name"] for it in self._extract_deleted_items(delete_lines, baseline_rows)]

    @staticmethod
    def _collect_rule_ids_from_rows(rows) -> list:
        """兜底：从合并后用例行里收集 R/S 编号（module_rules 缺失时保证自检表不缺失）"""
        ids, seen = [], set()
        for row in rows or []:
            for m in re.finditer(r"\b([RS])(\d+)\b", row):
                full = f"{m.group(1)}{m.group(2)}"
                if full not in seen:
                    seen.add(full)
                    ids.append(full)
        ids.sort(key=lambda x: (x[0], int(x[1:])))
        return ids

    def _locate_modified_via_llm(self, unmatched, baseline_rows, meta) -> Dict[str, str]:
        """
        修改项写的用例名与基线对不上时，让大模型做一次「修改项 → 基线用例名」定位。
        输入只有名称清单（体积极小）、输出只有若干行映射，因此不可能触发输出截断。
        返回 {"归一化修改项名": "基线中的用例名"}
        """
        if not unmatched or not baseline_rows:
            return {}
        name_list = "\n".join(
            f"{i+1}. {self._case_name(r)}" for i, r in enumerate(baseline_rows) if self._case_name(r)
        )
        items = "\n".join(
            f"[{i+1}] 修改用例名称：{it['name']}\n    修改后内容：{it['row'][:200]}"
            for i, it in enumerate(unmatched)
        )
        prompt = f"""
下面给出【历史基线用例名称清单】和【若干条「修改用例」】。
这些修改用例里写的被修改对象名称与基线名称不一致（可能改了措辞、少了修饰词），
请判断每条修改用例**实际对应基线中的哪一条用例**。

【历史基线用例名称清单】
{name_list}

【待定位的修改用例】
{items}

【输出格式】每行一条，不要输出任何多余文字；确实找不到对应则写 NONE：
[序号] -> 基线中对应的用例名称
"""
        try:
            raw = self.call_with_guard(prompt)
            meta["llm_locate_calls"] = meta.get("llm_locate_calls", 0) + 1
            logger.info(f"🤖 修改项兜底定位完成 | 待定位={len(unmatched)}条 | 返回长度={len(raw)}字")
        except Exception as e:
            logger.warning(f"⚠️ 修改项兜底定位失败（相关修改项降级为新增）: {str(e)}")
            return {}

        mapping = {}
        for m in re.finditer(r"\[(\d+)\]\s*[-—>→]+\s*(.+)", raw):
            idx = int(m.group(1)) - 1
            target = m.group(2).strip().strip("*`「」").strip()
            if 0 <= idx < len(unmatched) and target and target.upper() != "NONE":
                mapping[self._norm_case_name(unmatched[idx]["name"])] = target
        return mapping

    def merge_full_cases(
            self,
            history_cases: str,
            incremental_cases: str,
            module_rules: str = "",
            confirmed_deletions=None
    ):
        """
        增量定点归并：返回 (合并后的全量用例文本, 归并元信息)。

        归并顺序：以历史基线行为骨架 → 按名剔除删除项 → 按名定点替换修改项 →
        追加新增项（增量清单顺序）→ 代码统一输出 9 列表头 + 代码级编号覆盖自检表。

        删除确认闸门（2026-09-16 新增）：
        - `confirmed_deletions=None`（默认）→ **hold 模式**：本轮不执行任何删除，模型"建议删除"的用例
          原样保留在产物中，清单通过 `meta["pending_deletions"]` 回传前端等人工勾选。
          理由：删除是本链路里唯一「不可逆且无人工审阅」的动作，模型误判会静默少用例。
        - `confirmed_deletions=["用例名", ...]` → 只删除列表中的项（传空列表 = 全部保留）。
        """
        meta = {
            "mode": "point_merge", "baseline_rows": 0, "added": 0, "modified": 0,
            "deleted": 0, "unmatched_modified": 0, "llm_locate_calls": 0,
            "dropped_rows": 0, "columns_ok": True, "check_table": False,
            # 删除确认闸门 + 静默降级点的可观测字段（供前端提示与排查）
            "deletion_policy": "hold", "pending_deletions": [],
            "deleted_names": [], "unmatched_names": [], "skipped_duplicates": [],
        }

        base_rows = self._parse_case_rows(history_cases)
        meta["baseline_rows"] = len(base_rows)

        # 无历史基线 → 增量清单即全量，不必归并（也就不会无谓调模型）
        if not base_rows:
            meta["mode"] = "no_baseline"
            logger.info("ℹ️ 未召回历史基线，合并产物 = 增量清单原文（跳过归并）")
            return (incremental_cases or "").strip(), meta

        sections = self._split_incremental_sections(incremental_cases)
        meta["section_titles"] = sections.get("title_hits", 0)

        # 候选行来源：
        # - 识别到章节标题 → 只取「新增 / 修改」节；未归属章节的 loose 行一律忽略
        #   （否则模型自带的统计表 / 覆盖自检表会被当成用例追加进产物）
        # - 完全没识别到标题 → add + loose 合并，再由「是否命中基线用例名」归类（宽松策略）
        loose_used = sections.get("title_hits", 0) == 0
        add_candidates = list(sections["add"]) + (list(sections["loose"]) if loose_used else [])
        if not loose_used and sections["loose"]:
            logger.warning(
                f"⚠️ 增量清单有 {len(sections['loose'])} 行未归属任何章节，已忽略不并入产物"
                f"（通常是模型自带的统计表/自检表）"
            )

        def _clean(rows, tag):
            """行规整（补/并到 9 列）+ 粗筛：非用例形态的行丢弃并点名，保证产物列数一致"""
            kept, dropped = [], 0
            for row in rows:
                if not self._is_case_like_row(row):
                    dropped += 1
                    n = len(row.strip().strip("|").split("|"))
                    logger.warning(f"⚠️ 忽略非用例形态的表格行（{tag}，仅 {n} 列）：{row[:80]}")
                    continue
                kept.append(self._normalize_row(row))
            return kept, dropped

        add_candidates, drop_a = _clean(add_candidates, "新增节")
        modify_candidates, drop_m = _clean(sections["modify"], "修改节")
        meta["dropped_rows"] = drop_a + drop_m

        # ---- 1) 删除：按用例名从骨架中剔除（默认 hold，人工确认后才真删）----
        delete_items = self._extract_deleted_items(sections["delete"], base_rows)
        meta["pending_deletions"] = delete_items
        if confirmed_deletions is None:
            # hold 模式：一个都不删，把清单交给人工确认
            confirmed_keys = set()
            meta["deletion_policy"] = "hold"
        else:
            confirmed_keys = {
                self._norm_case_name(n) for n in (confirmed_deletions or []) if n
            }
            meta["deletion_policy"] = "applied"
        meta["deleted_names"] = [
            it["name"] for it in delete_items
            if self._norm_case_name(it["name"]) in confirmed_keys
        ]

        skeleton = []
        for row in base_rows:
            key = self._norm_case_name(self._case_name(row))
            if key and key in confirmed_keys:
                meta["deleted"] += 1
                continue
            skeleton.append(self._normalize_row(row))

        missing_del = [it["name"] for it in delete_items if not it.get("matched")]
        if missing_del:
            logger.warning(
                f"⚠️ 「建议删除」中有 {len(missing_del)} 条在基线里找不到同名用例（无法执行删除）: {missing_del}"
            )
        if delete_items and meta["deletion_policy"] == "hold":
            logger.info(
                f"⏸️ 定点归并-删除待人工确认 | 模型建议删除 {len(delete_items)} 条，"
                f"本轮全部保留在产物中 | 待确认={[it['name'] for it in delete_items]}"
            )
        elif meta["deleted"]:
            logger.info(
                f"🗑️ 定点归并-删除 {meta['deleted']} 条 | 已确认名称={meta['deleted_names']}"
            )

        # ---- 2) 修改：按用例名定点替换（未命中的走模型兜底）----
        pos = {self._norm_case_name(self._case_name(r)): i for i, r in enumerate(skeleton)}
        unmatched = []
        for row in modify_candidates:
            key = self._norm_case_name(self._case_name(row))
            if key and key in pos:
                skeleton[pos[key]] = row
                meta["modified"] += 1
            else:
                unmatched.append({"row": row, "name": self._case_name(row)})

        # 宽松策略：识别不到章节标题时，候选行里「命中基线名」的按修改处理
        if loose_used:
            loose_add = []
            for row in add_candidates:
                key = self._norm_case_name(self._case_name(row))
                if key and key in pos:
                    skeleton[pos[key]] = row
                    meta["modified"] += 1
                else:
                    loose_add.append(row)
            add_candidates = loose_add
            logger.info(
                f"ℹ️ 增量清单未识别到章节标题，已按用例名归类 | 改={meta['modified']} | 待追加={len(loose_add)}"
            )

        if unmatched:
            mapping = self._locate_modified_via_llm(unmatched, base_rows, meta)
            extra_rows = []
            for it in unmatched:
                target = mapping.get(self._norm_case_name(it["name"]))
                tkey = self._norm_case_name(target) if target else ""
                if tkey and tkey in pos:
                    skeleton[pos[tkey]] = it["row"]
                    meta["modified"] += 1
                else:
                    # 定位不到也不能丢内容：降级为新增追加，并在日志里点名
                    extra_rows.append(it["row"])
                    meta["unmatched_modified"] += 1
                    meta["unmatched_names"].append(it["name"])
                    logger.warning(f"⚠️ 修改项未能定位到基线用例，降级为新增追加：{it['name']}")
        else:
            extra_rows = []

        # ---- 3) 新增：追加到末尾（与骨架/已追加重名的跳过，避免重复）----
        #     重名跳过是「静默丢弃」，必须回传名字 —— 否则模型误把修改项放进新增节时，
        #     用户只会看到产物少了一条却不知为何。
        seen_keys = set(pos.keys())
        added_rows = []
        for row in add_candidates + extra_rows:
            key = self._norm_case_name(self._case_name(row))
            if key and key in seen_keys:
                if self._case_name(row):
                    meta["skipped_duplicates"].append(self._case_name(row))
                continue
            seen_keys.add(key)
            added_rows.append(row)
        meta["added"] = len(added_rows)
        if meta["skipped_duplicates"]:
            logger.info(
                f"ℹ️ 新增节中有 {len(meta['skipped_duplicates'])} 条与基线/已追加用例同名，已跳过："
                f"{meta['skipped_duplicates']}"
            )

        # ---- 4) 拼装：代码统一表头 + 骨架行 + 新增行 + segment_map + 代码级编号覆盖自检表 ----
        merged_rows = skeleton + added_rows
        expected_ids = self._extract_rule_ids(module_rules) or self._collect_rule_ids_from_rows(merged_rows)
        parts = [CASE_HEADER, CASE_SEPARATOR] + merged_rows
        # 归并后没有分片信息（骨架行来自历史产物、新增行来自变更清单），
        # 归属只能靠备注列的 `专属规则-R{n}` 标注重建 —— 必须重建，
        # 否则归并产物会丢掉 segment_map，评审线又退回「同编号跨片」的旧问题
        payload = segment_map.build(merged_rows, expected_ids=expected_ids, source="merge")
        if payload:
            parts.append("")
            parts.append(segment_map.render_marker(payload))
            meta["segment_map"] = segment_map.marker_stats(payload)
        else:
            logger.warning(
                "⚠️ 归并产物未能重建编号归属映射（用例备注列无 `专属规则-R{n}` 标注）"
                "⇒ 编号覆盖自检表退回扫实质列、评审退回顺序分片"
            )
        check = self._build_code_check_table(merged_rows, expected_ids, payload=payload)
        if check:
            parts.append(check)
            meta["check_table"] = True
        merged = "\n".join(parts)

        # ---- 5) 产物完整性自检：行单元格数必须为 9（列数异常 = 产物残缺，直接告警）----
        dist = {}
        for row in merged_rows:
            n = len(row.strip("|").split("|"))
            dist[n] = dist.get(n, 0) + 1
        bad = {k: v for k, v in dist.items() if k != 9}
        meta["columns_ok"] = not bad
        meta["columns_dist"] = dist
        if bad:
            logger.warning(
                f"⚠️ 合并产物列数自检异常（预期每行 9 列）| 异常分布={bad} | 请人工核对产物"
            )
        else:
            logger.info(f"✅ 合并产物列数自检通过 | {len(merged_rows)} 行全部 9 列")

        logger.info(
            f"✅ 定点归并完成 | 基线={meta['baseline_rows']} → 合并后={len(merged_rows)} 条 | "
            f"新增={meta['added']} | 修改={meta['modified']} | 删除={meta['deleted']} | "
            f"自检编号={len(expected_ids)}个 | 长度={len(merged)}字"
        )
        return merged, meta

    # ==================== Prompt 构造 ====================

    def get_prompt(self, input_context: Dict[str, Any]) -> str:
        """兼容旧调用：不指定编号范围（单次全量）"""
        return self._build_prompt(input_context, None, "full")

    def _build_prompt(
            self,
            input_context: Dict[str, Any],
            rule_ids=None,
            mode: str = "full"
    ) -> str:
        """构造 Prompt。mode 取值：
        - full：单次全量（通用交互 + 全部专属），用于无编号回退
        - common：仅通用交互用例（独立成片，消除首片过载）
        - numbered：仅指定编号的专属业务用例（编号片）
        """
        requirement = input_context.get("requirement", "")
        review_report = input_context.get("review_report", "")
        common_rules = input_context.get("common_rules", "")
        module_rules = input_context.get("module_rules", "")
        skill_ctx = input_context.get("skill_context", "")
        history_ctx = input_context.get("history_context", "")
        supplement_rules = input_context.get("supplement_rules", "")
        biz_module = input_context.get("biz_module", "")
        business_docs = input_context.get("business_context", "")

        # 通用规则兜底：未配置时明确要求按标准后台管理页面通用交互覆盖
        common_block = common_rules.strip() if common_rules and common_rules.strip() else (
            "（未加载到通用规则文档，请按标准后台管理列表页惯例自行覆盖以下通用交互："
            "查询筛选、重置、分页、列表展示、操作按钮、状态标识、空数据、异常场景）"
        )

        # 诊断日志：确认通用规则是否真的注入（排查通用校验项缺失问题的关键依据）
        logger.info(
            f"🧪 构造用例生成Prompt | biz_module={biz_module or '未指定'} | "
            f"通用规则={len(common_rules or '')}字 | 模块规则={len(module_rules or '')}字 | "
            f"技能样例={len(skill_ctx or '')}字 | 历史沉淀={len(history_ctx or '')}字 | "
            f"上传文档={len(business_docs or '')}字 | 补充约束={len(supplement_rules or '')}字 | "
            f"分片编号={rule_ids or '不分片'}"
        )
        if not common_rules:
            logger.warning(
                "⚠️ 通用规则为空，页面类用例可能缺失通用交互校验项，"
                "请检查 page_common_rules.md 是否已标记 rule_scope=common"
            )

        # ===== 分片约束块（按 mode 控制覆盖范围）=====
        segment_block = ""
        coverage_line = (
            "先输出通用交互用例（Step1 优先级1 的每一条校验项至少对应1条用例），"
            "再输出模块专属业务用例（从优先级2 的「核心业务规则」「字段约束」「状态流转」中逐条提取）。"
            "两者都不可遗漏 —— 通用规则不因存在专属规则而被省略。"
        )
        if mode == "common":
            coverage_line = (
                "本片仅输出【优先级1 通用交互规则】对应的通用交互用例，"
                "每一条通用校验项至少对应 1 条用例，禁止输出模块专属业务用例。"
            )
            segment_block = """
- 【分片生成约束】本片为「通用交互规则」独立片。你只负责输出通用交互用例：
  严格按【优先级1 通用交互规则】逐条覆盖（每一条通用校验项至少 1 条用例），
  覆盖查询筛选、重置、分页、列表展示、操作按钮、状态标识、空数据、异常场景等全部通用交互项。
  ⚠️ 本片禁止输出模块专属业务用例（模块专属规则用例由后续「编号片」单独输出）。
"""
        elif mode == "numbered":
            id_text = "、".join(rule_ids) if rule_ids else ""
            coverage_line = (
                "本片仅输出【优先级2 模块专属规则】中本片负责编号对应的专属业务用例，"
                "禁止输出通用交互用例（已在「通用交互片」覆盖）。"
            )
            segment_block = f"""
- 【分片生成约束】本片为「模块专属规则」编号片。你只负责输出以下规则编号对应的模块专属业务用例：{id_text}。
  ⚠️ 通用交互用例（查询筛选/重置/分页/列表/状态标识等）已在「通用交互片」覆盖，本片禁止重复输出。
  对每个编号，严格按【优先级2】中该编号条目的「覆盖要求」列补齐各业务态场景（正向/异常/边界/状态），
  生成条数不低于该条目的「必生成数」。
- 【备注列硬约束】本片每条用例的「备注」列**必须**以 `专属规则-R{{n}}` 形式标注其依据的编号
  （如 `专属规则-R3`；S 类编号写 `专属规则-S2`），多条依据用「、」分隔。
  这是框架核对「该编号是否真的生成了用例」的唯一依据，漏写会导致该编号在自检表中被判为未覆盖。
"""

        # ===== Step4 自检表块 =====
        if mode == "numbered":
            check_rows = "\n".join(f"| {rid} | 是/否 | xxx |" for rid in (rule_ids or []))
            self_check_block = f"""
## 编号覆盖自检表（逐条核对本片负责的规则编号是否均已生成用例）
| 规则编号 | 是否覆盖 | 对应用例名称 |
|--------|--------|----------|
{check_rows}

若某项为「否」，说明该规则条目缺失，请回到 Step3 补充对应用例后再输出。
"""
        else:
            self_check_block = """
## 通用规则覆盖自检表
| 通用规则分类 | 是否覆盖 | 对应用例编号 |
|--------|----------|----------|
| 查询筛选区 | 是/否 | xxx |
| 重置按钮 | 是/否 | xxx |
| 分页组件 | 是/否 | xxx |
| 列表展示 | 是/否 | xxx |
| 操作按钮 | 是/否 | xxx |
| 状态标识 | 是/否 | xxx |
| 空数据场景 | 是/否 | xxx |
| 异常场景 | 是/否 | xxx |

若某项为「否」，说明该通用校验项缺失，请回到 Step3 补充对应用例后再输出。
"""

        return f"""
你是资深测试用例设计专家。请严格按 Step0 → Step4 的顺序执行，不得跳步。

======================================================================
# Step 0｜场景识别（先判断模式，再加载规则）
======================================================================
请先判断本次需求属于哪种模式，判断结果决定规则加载方式：
- 【页面功能模式】：需求指明了具体页面/模块（如"卡BIN池管理页面""原数据文件历史页"），
                   或描述了列表、查询、按钮、弹窗等UI元素
- 【需求文档模式】：需求为PRD/接口/纯业务规则描述，未指定具体页面

======================================================================
# Step 1｜规则加载（严格按优先级，冲突时高优先级覆盖低优先级）
======================================================================
【优先级1｜通用交互规则 —— 强制加载，逐条覆盖，不可遗漏】
{common_block}

【优先级2｜{biz_module or '目标'}模块专属业务规则 —— 叠加在通用规则之上】
{module_rules.strip() if module_rules and module_rules.strip() else "（未加载到模块专属规则）"}

【优先级3｜输出格式规范与标准样例】
{skill_ctx.strip() if skill_ctx and skill_ctx.strip() else "（未加载到技能库样例，请按 Step3.4 硬约束输出）"}

======================================================================
# Step 2｜业务输入信息
======================================================================
1. 原始需求：
{requirement}

2. 需求评审报告（补充遗漏测试点与风险点）：
{review_report if review_report else "（无评审报告）"}

3. 上传文档内容：
{business_docs if business_docs and business_docs.strip() else "（无上传文档）"}

4. 历史相似用例参考（仅供业务逻辑参考，禁止照搬其表格结构）：
{history_ctx if history_ctx else "（无历史沉淀）"}

5. 用户追加的补充约束（本次最高优先级，与任何规则冲突时以此为准）：
{supplement_rules if supplement_rules else "（无补充约束）"}

======================================================================
# Step 3｜生成要求（硬约束，必须全部满足）
======================================================================
## 3.1 覆盖范围
- 若为【页面功能模式】：{coverage_line}
- 若为【需求文档模式】：应用等价类划分、边界值、场景法、异常流设计用例，覆盖正常/边界/异常三类。
{segment_block}

## 3.2 业务硬约束（违反任意一条即为错误用例）
- 从【优先级2 模块专属规则】中提取全部「核心业务规则」「字段约束」「状态流转」作为硬约束，
  每一条硬约束至少要有1条对应用例（正向 + 反向各一条为佳）。
- 状态类需求必须覆盖完整状态枚举，并为每个状态设计对应的操作按钮可用性校验。
- 涉及数值/长度/格式的业务字段，必须覆盖边界值（最小值、最大值、越界值、非法字符）。

## 3.3 用例质量硬约束
- 一条用例只验证一个测试点，禁止把多个验证点塞进同一条用例。
- 预期结果必须可量化、可验证，禁止"正常显示""无异常"这类模糊描述。
- 前置条件必须写清数据/状态前提，不能为空（无前置则填"无"）。
- 业务规则不明确时，在用例中标注「待确认」，禁止编造业务逻辑。

## 3.4 输出格式硬约束
- 表格列数、列名、列顺序严格遵循【优先级3】的标准样例；样例缺失时按
  用例名称|模块|标签|前置条件|步骤描述|预期结果|编辑模式|备注|用例等级 输出。
- 禁止模仿【历史相似用例参考】的旧表格结构。
- 步骤描述与预期结果按 [步骤编号]+内容 组织，如 [1]输入账号 [2]点击登录。
- 优先级：P0（核心主流程）、P1（边界/异常/分支）、P2（UI/体验/弱相关）。
- ⚠️ 单元格内容里若出现竖线字符 `|`（如枚举值「使用中|已弃用」、XPath 的 //a|//b），
  **必须写成转义形式 `\|`**。裸竖线会被当成列分隔符，使该行后续列整体右移、内容错乱。
- 除用例表格和末尾自检表外，禁止输出任何前言、解释、总结性文字。

======================================================================
# Step 4｜输出自检表（放在用例表格之后，用于校验规则是否全覆盖）
======================================================================
{self_check_block}
"""

    def parse_output(self, raw_response: str) -> Dict[str, Any]:
        return {
            "test_cases": raw_response,
            "stage": "test_cases_completed"
        }

    # ==================== 执行入口：编号分片 ====================

    def run(self, input_context: Dict[str, Any]) -> Dict[str, Any]:
        """
        用例生成执行入口（override BaseSkill.run）：
        1. 从模块规则提取 R/S 编号；
        2. 无编号 → 走单次全量生成，产物同样过「9 列归一 + 代码表头 + 自检」；
        3. 有编号 → 「通用交互片」+「编号片」分别生成，最后合并为单一全量表格 + 单一张自检表。
           （通用交互用例独立成片，避免与专属业务用例挤在同一片导致输出超限截断）

        对 flow_scheduler 完全透明：仍返回 {"test_cases": 全量, "stage": ...}。
        """
        module_rules = input_context.get("module_rules", "") or ""
        rule_ids = self._extract_rule_ids(module_rules)

        # 无 R/S 编号规则（非页面规则或规则未编号）→ 回退单次全量生成。
        # 注意：回退路径**也必须统一组装**，否则产物就是模型原文（没有代码表头、没有列归一、
        # 没有列数自检），9 列契约在这条路径上完全失效。
        if not rule_ids:
            logger.info("ℹ️ 模块规则未提取到 R/S 编号，走单次全量生成（产物同样做 9 列归一）")
            res = super().run(input_context)
            raw_text = (res or {}).get("test_cases", "") or ""
            assembled = self._assemble_single_pass(raw_text)
            if assembled:
                res["test_cases"] = assembled
                logger.info(
                    f"🧱 单次全量产物已归一化 | 原={len(raw_text)}字 → {len(assembled)}字"
                )
            else:
                logger.warning(
                    "⚠️ 单次全量产物未识别到用例表格行，保留模型原文（请人工核对列结构）"
                )
            return res

        outputs, labels = [], []
        # 与 outputs 一一对应的分片元信息：segment_map 靠它把每行归属回它所属的编号片
        segments_meta = []

        # 片 0：通用交互用例（独立成片，消除首片过载导致的截断）
        logger.info("🧩 生成通用交互片 | 通用交互用例独立输出")
        label = "通用交互片"
        text, ok = self._call_segment(self._build_prompt(input_context, None, "common"), label)
        outputs.append(text)
        labels.append(label)
        segments_meta.append({"label": label, "type": "common", "rule_ids": []})
        failed_segments = [] if ok else [label]

        # 编号片：按 SEGMENT_SIZE 均分，只输出专属业务用例
        chunks = self._chunk(rule_ids, self.SEGMENT_SIZE)
        logger.info(
            f"🧩 编号共 {len(rule_ids)} 条，按每片 {self.SEGMENT_SIZE} 条拆为 {len(chunks)} 片专属生成"
        )
        for i, chunk in enumerate(chunks):
            label = f"编号片[{i+1}/{len(chunks)}]({','.join(chunk)})"
            logger.info(f"🧩 编号片生成 [{i+1}/{len(chunks)}] | 编号={chunk}")
            text, ok = self._call_segment(
                self._build_prompt(input_context, chunk, "numbered"), label
            )
            outputs.append(text)
            labels.append(label)
            segments_meta.append({"label": label, "type": "numbered", "rule_ids": list(chunk)})
            if not ok:
                failed_segments.append(label)

        # 只有「全部片都未产出」才算整批失败；部分失败照实合并并标注缺哪几片。
        # 原实现是「任一片抛异常 → 整个 run() 抛出」，已成功分片的成果全部作废，用户白等一场。
        if failed_segments and len(failed_segments) == len(outputs):
            raise Exception(
                f"分片生成全部失败（{len(outputs)} 片均未产出）：{'；'.join(failed_segments)}"
            )
        if failed_segments:
            logger.warning(
                f"⚠️ {len(failed_segments)}/{len(outputs)} 个分片未产出，"
                f"产物会缺少对应编号的用例 | 未产出={failed_segments}"
            )

        merged = self._merge_segments(
            outputs, expected_rule_ids=rule_ids, failed_segments=failed_segments,
            segments_meta=segments_meta,
        )
        if not merged:
            raise Exception(
                "分片生成未得到任何用例行（模型未产出用例表格），请重试或检查规则文件"
            )
        logger.info(f"✅ 分片生成合并完成 | 总片数={len(outputs)} | 合并后长度={len(merged)}字")
        return self.parse_output(merged)

    def _assemble_single_pass(self, raw_text: str) -> str:
        """
        无 R/S 编号（单次全量）时的统一组装：把模型原文里的用例行归一化为 9 列，
        并补上代码表头与编号自检 —— 与分片路径共用同一套 9 列契约。

        识别不到任何用例行时返回 ""（调用方保留模型原文，避免把内容洗没）。
        """
        rows = []
        raw_dist = {}
        for line in (raw_text or "").split("\n"):
            s = line.strip()
            if not s.startswith("|") or self._is_self_check_row(s):
                continue
            if not self._is_case_like_row(s):
                continue
            n_cols = len(split_md_row(s))
            raw_dist[n_cols] = raw_dist.get(n_cols, 0) + 1
            rows.append(self._normalize_row(s))
        if not rows:
            return ""

        odd_raw = {k: v for k, v in raw_dist.items() if k != 9}
        if odd_raw:
            logger.warning(
                f"⚠️ 模型输出的用例行存在非 9 列（已按 9 列归一，但内容可能已错位，请人工核对）| "
                f"原始列数分布={odd_raw}"
            )

        parts = [CASE_HEADER, CASE_SEPARATOR] + rows
        expected_ids = self._collect_rule_ids_from_rows(rows)
        # 单次全量路径没有分片信息，靠备注列 `专属规则-R{n}` 与实质列扫描建立归属映射，
        # 保证下游（自检表 / 评审按编号装箱）与分片路径口径一致
        payload = segment_map.build(rows, expected_ids=expected_ids, source="single_pass")
        if payload:
            parts.append("")
            parts.append(segment_map.render_marker(payload))
        check = self._build_code_check_table(rows, expected_ids, payload=payload) if expected_ids else ""
        if check:
            parts.append(check)

        dist = {}
        for row in rows:
            n = len(split_md_row(row))
            dist[n] = dist.get(n, 0) + 1
        logger.info(f"🧱 单次全量产物组装完成 | 用例={len(rows)}条 | 列数分布={dist}")
        return "\n".join(parts)

    def _call_segment(self, prompt: str, label: str):
        """
        单片 LLM 调用（含自动重试）。

        :return: (text, ok) —— ok=False 表示本片未产出（网络失败 / 空返回 / 输出截断），
                 由 `_merge_segments` 如实报告「哪几片缺失」，而不是让整批生成直接 500。

        与原实现的两点关键差别：
        1. `LLMTruncationError` 属**确定性失败**（相同入参重试结果不变）⇒ 直接放弃本片、不重试。
           原实现把它当普通异常重试 max_retry 次，白烧调用且最终仍然整批失败。
        2. 模型返回空内容不再算「成功」——原实现视空串为成功，`_merge_segments` 再静默跳过，
           结果是「静默少片」：产物缺用例，却看起来一切正常。
        """
        last_err = ""
        for attempt in range(self.max_retry + 1):
            try:
                out = self._call_llm(prompt)
                if out and out.strip():
                    return out, True
                last_err = "模型返回空内容"
                logger.warning(f"⚠️ {label} 第 {attempt+1} 次返回空内容（视为本片失败）")
            except LLMTruncationError:
                logger.error(
                    f"❌ {label} 输出被截断（finish_reason=length），本片放弃且不重试"
                    f"（相同入参重试结果不会改变）"
                )
                return "", False
            except Exception as e:
                last_err = str(e)
                logger.warning(f"⚠️ {label} 第 {attempt+1} 次调用失败: {last_err}")
        logger.error(f"❌ {label} 重试 {self.max_retry + 1} 次后仍未产出：{last_err}")
        return "", False

    # 增量第一步生成方法（输出的是「变更清单」，本身就是小体量输出，无需分片）
    # 合并全量另见 merge_full_cases（定点归并，避免重写全量导致截断）
    def get_incremental_prompt(
        self,
        history_cases: str,
        change_desc: str,
        impact_analysis: str = None,
        common_rules: str = "",
        module_rules: str = ""
    ) -> str:
        impact_block = ""
        if impact_analysis and impact_analysis.strip():
            impact_block = f"""
【影响面分析结果】（已由用户确认，生成时严格据此界定变更范围）
{impact_analysis.strip()}
"""
        # 业务规则注入：增量模式此前完全不注入规则，导致「改了 skill 也不生效」，
        # 现与完整/快速模式口径对齐，注入通用规则 + 该页面专属规则全文
        rules_block = ""
        if module_rules and module_rules.strip():
            rules_block += f"""
【本页面专属业务规则】（生成/修改用例必须严格遵守；R/S 编号用于覆盖自检）
{module_rules.strip()}
"""
        if common_rules and common_rules.strip():
            rules_block += f"""
【通用交互规则】（所有页面通用，增量新增/修改用例同样必须覆盖，不可遗漏）
{common_rules.strip()}
"""
        if not rules_block:
            rules_block = "\n【业务规则】（本次未取到规则全文，请严格依据历史用例的规则口径保持一致）\n"

        # 历史产物里带有 segment_map 注释行（机器可读映射），对模型是纯噪音 ⇒ 剔除后再入 prompt。
        # 归并（merge_full_cases）走 _parse_case_rows，只认 `|` 开头行，天然不受影响。
        history_text = segment_map.strip_marker(history_cases or "")

        return f"""
你是资深测试用例变更分析专家，请基于历史版本用例、业务规则与本次变更说明，输出增量测试用例。
{rules_block}
【历史版本全量用例】
{history_text if history_text.strip() else "（未召回历史版本用例，请按全新模块生成）"}

【本次变更说明】
{change_desc}
{impact_block}
【生成要求】
1. 仅针对变更点生成增量测试用例，未变更功能禁止输出
2. 若提供【影响面分析结果】，其「受影响的历史用例」必须逐条在本输出中体现（新增/修改/删除任一形式），不得遗漏
3. 用例格式、字段、编号规则必须与历史版本完全一致（列数、列名、列顺序均不可变）
4. 新增/修改的用例必须符合【本页面专属业务规则】中各 R/S 编号的覆盖要求（含必生成数）；
   每条用例的「备注」列必须标注其依据的 R/S 编号（如「按 R3 校验」）
5. 输出必须分为三个部分（编号连续，废弃用例的编号不再复用）：
   ## 一、新增用例
   ## 二、修改用例（标注修改点说明）
   ## 三、建议删除用例（标注删除原因）

   【三个部分的硬性书写规则 —— 框架按此做机械归并，写错会导致合并错位】
   5.1 「## 二、修改用例」中的每一行，其**第一个单元格（用例名称列）必须与【历史版本全量用例】
       中该用例的名称逐字完全一致**（含标点与空格）。这是框架定位「改的是哪一条」的唯一依据，
       **不允许**在名称列写改后的新名称、不允许加「（调整）」「（V2）」之类后缀、不允许精简措辞。
       本次要修改的内容请写在「前置条件 / 步骤描述 / 预期结果 / 备注」等其余列；
       若确实需要变更用例名称本身，请保持名称列原文不变，并在「备注」列写明「建议改名：<新名称>」。
   5.2 「## 三、建议删除用例」中，**每条被删用例必须单独占一行并明确写出用例名称**，
       名称同样必须与历史版本逐字一致，格式为 `| 用例名称 | 删除原因 |` 或
       `- 用例名称（删除原因）`。禁止只写「部分冗余用例」「若干无效用例」这类概括性描述 ——
       含糊描述会被框架丢弃，导致该删的没删。
   5.3 「## 一、新增用例」中**不得出现与历史版本同名的用例**（框架按名称去重，同名会被跳过并丢失）。
       判断标准：不只看名称字面是否相同，也要看语义是否重复 —— 若某条用例的校验目标已被历史版本覆盖，
       应放入「二、修改用例」而非「一、新增用例」。
   5.4 三个部分的表格**都必须是完整的 9 列**，列名与顺序与历史版本完全一致，不得缺列、不得合并单元格。

6. 用例粒度、优先级规则和历史版本保持统一
7. 每条用例的预期结果必须可量化可验证；业务规则不明确处标注「待确认」，禁止编造
"""
