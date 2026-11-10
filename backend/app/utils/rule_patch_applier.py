# -*- coding: utf-8 -*-
"""
@File    : rule_patch_applier.py
@Time    : 2026/9/15
@Author  : tanlei
"""
import os
import re
import logging
import shutil
import difflib
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple

import yaml

from app.core.config import config
from app.services.plugin_loader import parse_markdown_front_matter

logger = logging.getLogger("RULE_PATCH")


# 表格「编号」列：R1 / S12 / F3
ID_CELL_RE = re.compile(r"^\|\s*([RSF])(\d+)\s*\|")

# front matter 块（用于「需求变更改动」打标）
FRONT_MATTER_RE = re.compile(r"^---\s*\n(.*?)\n---[ \t]*\n?", re.DOTALL)

# 「改动来源」标记值：需求变更（规则补丁）——规则管理页据此展示标注
CHANGE_ORIGIN_REQUIREMENT = "requirement_change"


def display_width(s: str) -> int:
    """估算字符串显示宽度（CJK/全角记 2 列），用于对齐表格"""
    w = 0
    for ch in s:
        w += 2 if ord(ch) > 0x2E7F else 1
    return w


class RulePatchApplier:
    """
    规则文件「补丁式定点更新」引擎（方案 C 的执行侧）。

    为什么不做「模型全量重写规则文件」：
      编号（R/S）是分片生成、编号自检、受影响范围识别、补丁定位的公共锚点。
      全文重写会导致编号漂移（失锚）、静默丢规则、格式漂移（_extract_rule_ids 宽松正则不报错
      → 静默退化为无分片单次生成）。故这里只做**定点补丁**，且**编号只增不改**。

    支持三种操作：
      - add       : 在对应编号表末尾续一个新编号（自动取 next id）
      - modify    : 定位某编号行，只改指定列（编号本身不变）
      - deprecate : 标记废弃。把编号列 `R7` 改写为 `R-7` 并在名称列加「【已废弃】」前缀。
                    这样 `\\b[RS]\\d+\\b` 不再匹配该条，它会自动退出分片与自检；
                    同时编号文本仍留在文档里可追溯（不物理删除，避免历史用例失锚）。

    安全措施：
      - 应用前先备份到 references/_versions/{文件名}.{时间戳}.bak（`.bak` 不会被 plugin_loader 载入）
      - 返回 applied / rejected 明细 + 文本 diff，供前端人工确认
    """

    def __init__(self, skill_name: str = "req-test-cases"):
        self.skill_name = skill_name
        self.skill_dir = os.path.join(config.skills_dir, skill_name)
        self.refs_dir = os.path.join(self.skill_dir, "references")
        self.backup_dir = os.path.join(self.refs_dir, "_versions")

    # ==================== 1. 定位规则文件 ====================
    def locate_rule_files(self, module: str, page: str) -> List[Dict[str, str]]:
        """
        找出该 (模块, 页面) 对应的规则文件（依据 front matter 的 biz_module + page，精确匹配）

        :return: [{"path", "filename", "biz_module", "page", "rule_scope"}]
        """
        module = (module or "").strip()
        page = (page or "").strip()
        found = []
        if not os.path.isdir(self.refs_dir):
            logger.warning(f"⚠️ references 目录不存在 | {self.refs_dir}")
            return found

        for root, dirs, files in os.walk(self.refs_dir):
            # 跳过备份目录
            dirs[:] = [d for d in dirs if d != "_versions"]
            for fn in files:
                if not fn.lower().endswith(".md"):
                    continue
                path = os.path.join(root, fn)
                fm = parse_markdown_front_matter(path)
                biz = str(fm.get("biz_module", "") or "").strip()
                pg = str(fm.get("page", "") or "").strip()
                if biz != module:
                    continue
                if pg != page:
                    continue
                found.append({
                    "path": path,
                    "filename": fn,
                    "biz_module": biz,
                    "page": pg,
                    "rule_scope": str(fm.get("rule_scope", "") or "").strip(),
                })
        logger.info(
            f"🔎 定位规则文件 | module={module} | page={page or '模块层'} | 命中={len(found)} | "
            f"文件={[f['filename'] for f in found]}"
        )
        return found

    # ==================== 2. 解析表格与编号行 ====================
    @staticmethod
    def _cells(line: str) -> List[str]:
        return [c.strip() for c in line.strip().strip("|").split("|")]

    @staticmethod
    def parse_tables(text: str) -> List[Dict[str, Any]]:
        """把文本切成若干 markdown 表格块（连续以 | 开头的行视为一块）"""
        lines = text.splitlines()
        tables = []
        i = 0
        while i < len(lines):
            if lines[i].strip().startswith("|"):
                start = i
                while i < len(lines) and lines[i].strip().startswith("|"):
                    i += 1
                tables.append({"start": start, "end": i - 1, "lines": lines[start:i]})
            else:
                i += 1
        return tables

    def parse_rule_rows(self, text: str) -> List[Dict[str, Any]]:
        """解析所有「编号行」（R/S/F），返回编号、前缀、所在表、行号、各列单元格"""
        rows = []
        for t in self.parse_tables(text):
            block = t["lines"]
            if len(block) < 2:
                continue
            header = self._cells(block[0])
            for off in range(2, len(block)):     # 跳过表头 + 分隔行
                m = ID_CELL_RE.match(block[off])
                if not m:
                    continue
                rows.append({
                    "id": f"{m.group(1)}{m.group(2)}",
                    "prefix": m.group(1),
                    "num": int(m.group(2)),
                    "cells": self._cells(block[off]),
                    "header": header,
                    "line_no": t["start"] + off,
                    "table_start": t["start"],
                    "table_end": t["end"],
                })
        return rows

    def next_id(self, text: str, prefix: str) -> str:
        """该前缀下的下一个可用编号（只增不改：取现有最大值 +1）"""
        nums = [r["num"] for r in self.parse_rule_rows(text) if r["prefix"] == prefix]
        return f"{prefix}{(max(nums) + 1) if nums else 1}"

    # ==================== 3. 行渲染（保持表格对齐） ====================
    @staticmethod
    def _render_row(cells: List[str], widths: List[int]) -> str:
        parts = []
        for i, c in enumerate(cells):
            w = widths[i] if i < len(widths) else display_width(c)
            pad = max(0, w - display_width(c))
            parts.append(c + " " * pad)
        return "| " + " | ".join(parts) + " |"

    @staticmethod
    def _column_widths(lines: List[str]) -> List[int]:
        widths: List[int] = []
        for ln in lines:
            cs = [c.strip() for c in ln.strip().strip("|").split("|")]
            for i, c in enumerate(cs):
                w = display_width(c)
                if i < len(widths):
                    widths[i] = max(widths[i], w)
                else:
                    widths.append(w)
        return widths

    # ==================== 4. 应用补丁 ====================
    def apply(self, text: str, patches: List[Dict[str, Any]]) -> Tuple[str, List[Dict], List[Dict]]:
        """
        对规则文本应用补丁。

        :param patches: [{op, rule_id?, table?, cells?, field?, new?, old?, reason?}]
        :return: (new_text, applied, rejected)
        """
        lines = text.splitlines()
        applied: List[Dict[str, Any]] = []
        rejected: List[Dict[str, Any]] = []
        insertions: List[Tuple[int, str]] = []     # (插入位置, 行内容)，最后按行号倒序插入
        self._pending_new_ids: List[str] = []      # 本批 add 已分配的编号（避免重复）

        for idx, p in enumerate(patches or []):
            op = str(p.get("op", "")).strip().lower()
            if op not in ("add", "modify", "deprecate"):
                rejected.append({"patch": p, "reason": f"未知操作 op={p.get('op')}"})
                continue

            # 每次都基于「当前 lines」重新解析，保证前面补丁的改动可见
            rows = self.parse_rule_rows("\n".join(lines))

            # ---------- add ----------
            if op == "add":
                prefix = str(p.get("table") or "").strip().upper()
                rid = str(p.get("rule_id") or "").strip().upper()
                if rid and rid[0] in "RSF":
                    prefix = rid[0]
                if prefix not in ("R", "S", "F"):
                    rejected.append({"patch": p, "reason": "add 需要指定 table（R/S/F）或合法 rule_id"})
                    continue
                same_prefix = [r for r in rows if r["prefix"] == prefix]
                if not same_prefix:
                    rejected.append({"patch": p, "reason": f"未找到 {prefix} 编号表，无法追加"})
                    continue
                anchor = max(same_prefix, key=lambda r: r["table_end"])
                table_start, table_end, header = anchor["table_start"], anchor["table_end"], anchor["header"]
                # 同一批补丁里连续 add 时编号要接着涨（插入延后执行，无法从文本重新推导）
                new_id = rid
                if not new_id:
                    base = max(r["num"] for r in same_prefix)   # 该前缀已有编号的最大值
                    used = [int(x[1:]) for x in self._pending_new_ids if x.startswith(prefix)]
                    new_id = f"{prefix}{max([base] + used) + 1}"
                self._pending_new_ids.append(new_id)
                new_cells = [new_id] + [str(c) for c in (p.get("cells") or [])]
                if len(new_cells) < len(header):
                    new_cells += [""] * (len(header) - len(new_cells))
                new_cells = new_cells[:len(header)]
                widths = self._column_widths(lines[table_start: table_end + 1])
                insertions.append((table_end + 1, self._render_row(new_cells, widths)))
                applied.append({
                    "op": op, "rule_id": new_id, "table": prefix,
                    "cells": new_cells, "reason": p.get("reason", ""),
                })
                continue

            # ---------- modify / deprecate ----------
            rid = str(p.get("rule_id") or "").strip().upper()
            if not rid:
                rejected.append({"patch": p, "reason": f"{op} 需要 rule_id"})
                continue
            row = next((r for r in rows if r["id"] == rid), None)
            if not row:
                rejected.append({"patch": p, "reason": f"未找到编号 {rid}（已废弃的编号会写成 {rid[0]}-{rid[1:]}）"})
                continue

            widths = self._column_widths(
                lines[row["table_start"]: row["table_end"] + 1]
            )
            cells = list(row["cells"])

            if op == "modify":
                field = str(p.get("field", "") or "").strip()
                if not field:
                    rejected.append({"patch": p, "reason": "modify 需要 field（列名）"})
                    continue
                if field not in row["header"]:
                    rejected.append({
                        "patch": p, "reason": f"列名 '{field}' 不在表头 {row['header']} 中"})
                    continue
                ci = row["header"].index(field)
                if ci == 0:
                    rejected.append({"patch": p, "reason": "编号列不允许修改（编号只增不改）"})
                    continue
                old_val = cells[ci] if ci < len(cells) else ""
                cells[ci] = str(p.get("new", ""))
                lines[row["line_no"]] = self._render_row(cells, widths)
                applied.append({
                    "op": op, "rule_id": rid, "field": field,
                    "old": old_val, "new": cells[ci], "reason": p.get("reason", ""),
                })

            else:  # deprecate
                # 编号列 R7 -> R-7，使 \b[RS]\d+\b 不再命中 → 自动退出分片与自检
                cells[0] = f"{rid[0]}-{rid[1:]}"
                if len(cells) > 1:
                    if not cells[1].startswith("【已废弃】"):
                        cells[1] = "【已废弃】" + cells[1]
                lines[row["line_no"]] = self._render_row(cells, widths)
                applied.append({
                    "op": op, "rule_id": rid, "new_id": cells[0],
                    "reason": p.get("reason", ""),
                })

        # 统一执行插入：先按行号降序、再倒序插入。
        # 倒序的目的是让**同一位置的多条 add** 保持补丁原有先后顺序（否则后插的会跑到前面）
        insertions.sort(key=lambda x: -x[0])
        for pos, content in reversed(insertions):
            lines.insert(pos, content)

        return "\n".join(lines), applied, rejected

    # ==================== 4.5 「需求变更改动」打标 ====================
    @staticmethod
    def stamp_change_origin(
        text: str, applied: List[Dict[str, Any]], change_note: str = ""
    ) -> Tuple[str, Dict[str, Any]]:
        """
        在规则文本的 front matter 里打上「本次由需求变更改动」的标记，返回 (新文本, 标记信息)。

        为什么存在：规则补丁是**需求变更驱动**的规则改动，和「人工上传/编辑的规则」来源不同。
        规则管理页需要能一眼看出「哪些规则是被需求变更改过的」——不落盘就无从追溯，
        所以把来源标记写进 front matter（只增键、不动既有键，plugin_loader 只读
        biz_module / page / rule_scope / priority / description，多出的键完全被忽略 ⇒ 零风险）。

        写入的键：
          last_change_origin = requirement_change
          last_change_at     = 本次改动时间
          last_change_note   = 变更描述（截断 200 字）
          changed_rule_ids   = 历史累计被需求变更改过的编号（幂等合并、去重、保序）

        无 front matter 时**不擅自补一段**（可能改变文件语义），只记日志跳过。
        """
        m = FRONT_MATTER_RE.match(text or "")
        if not m:
            logger.warning("⚠️ 规则文件无 front matter，「需求变更改动」标记已跳过")
            return text, {}

        try:
            fm = yaml.safe_load(m.group(1)) or {}
            if not isinstance(fm, dict):
                fm = {}
        except Exception as e:
            logger.warning(f"⚠️ front matter 解析失败，跳过改动标记 | {str(e)}")
            return text, {}

        ids: List[str] = []
        for a in applied or []:
            rid = str(a.get("rule_id") or a.get("new_id") or "").strip()
            # deprecate 的 new_id 形如 R-7，标记里记回原编号 R7 更便于人读
            if rid.startswith(("R-", "S-", "F-")):
                rid = rid[0] + rid[2:]
            if rid and rid not in ids:
                ids.append(rid)

        old = fm.get("changed_rule_ids") or []
        if isinstance(old, str):
            old = [old]
        merged: List[str] = [str(x) for x in old]
        for r in ids:
            if r not in merged:
                merged.append(r)

        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        fm["last_change_origin"] = CHANGE_ORIGIN_REQUIREMENT
        fm["last_change_at"] = ts
        if (change_note or "").strip():
            fm["last_change_note"] = " ".join((change_note or "").split())[:200]
        fm["changed_rule_ids"] = merged

        body = yaml.safe_dump(fm, allow_unicode=True, sort_keys=False, default_flow_style=False)
        new_text = "---\n" + body + "---\n" + (text or "")[m.end():]
        info = {
            "origin": CHANGE_ORIGIN_REQUIREMENT,
            "last_change_at": ts,
            "note": fm.get("last_change_note", ""),
            "changed_rule_ids": merged,
            "changed_rule_ids_this_time": ids,
        }
        logger.info(f"🏷️ 已打「需求变更改动」标记 | 本次编号={ids} | 累计={merged}")
        return new_text, info

    # ==================== 5. 落盘（备份 + 热重载） ====================
    def apply_to_file(
        self,
        file_path: str,
        patches: List[Dict[str, Any]],
        reload_skills: bool = True,
        change_note: str = "",
        stamp_origin: bool = True,
    ) -> Dict[str, Any]:
        """
        把补丁应用到指定规则文件：备份 → 定点替换 → 打「需求变更改动」标记 → 热重载知识库

        :param change_note: 本次需求变更描述，写入 front matter 便于追溯
        :param stamp_origin: 是否打「需求变更改动」标记（默认打；置 False 则行为与旧版一致）
        """
        if not os.path.exists(file_path):
            return {"ok": False, "message": f"规则文件不存在: {file_path}", "applied": [], "rejected": []}

        with open(file_path, "r", encoding="utf-8") as f:
            original = f.read()

        new_text, applied, rejected = self.apply(original, patches)
        if not applied:
            logger.warning(f"⚠️ 补丁全部被拒绝，未改动文件 | {os.path.basename(file_path)}")
            return {"ok": False, "message": "没有可应用的补丁（全部被拒绝）",
                    "applied": [], "rejected": rejected}

        # 打标放在写盘之前，让 diff 也能体现标记变化（备份仍是补丁前的原始内容）
        stamp = {}
        if stamp_origin:
            new_text, stamp = self.stamp_change_origin(new_text, applied, change_note)

        # 备份（.bak 不会被 plugin_loader 的 *.md 扫描命中）
        os.makedirs(self.backup_dir, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d%H%M%S")
        backup_path = os.path.join(self.backup_dir, f"{os.path.basename(file_path)}.{ts}.bak")
        shutil.copy2(file_path, backup_path)

        with open(file_path, "w", encoding="utf-8") as f:
            f.write(new_text)

        diff = "\n".join(difflib.unified_diff(
            original.splitlines(), new_text.splitlines(),
            fromfile="before", tofile="after", lineterm=""
        ))

        logger.info(
            f"🩹 规则补丁已应用 | 文件={os.path.basename(file_path)} | "
            f"应用={len(applied)} | 拒绝={len(rejected)} | 备份={backup_path}"
        )

        reload_result = {}
        if reload_skills:
            # 规则文件改盘不自动生效（load_all_skills 只在启动/沉淀通用时调用），必须显式重载
            from app.services.plugin_loader import load_all_skills
            reload_result = load_all_skills()
            logger.info(f"🔄 规则补丁后知识库重载 | 文档数={reload_result.get('count', 0)}")

        return {
            "ok": True,
            "message": f"已应用 {len(applied)} 条补丁" + (f"，拒绝 {len(rejected)} 条" if rejected else ""),
            "file": file_path,
            "backup": backup_path,
            "applied": applied,
            "rejected": rejected,
            "diff": diff,
            "stamp": stamp,
            "reloaded": bool(reload_result),
            "loaded_docs": reload_result.get("count", 0),
        }


# 全局单例
rule_patch_applier = RulePatchApplier()
