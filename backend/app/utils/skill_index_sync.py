# -*- coding: utf-8 -*-
"""
@File    : skill_index_sync.py
@Time    : 2026/9/29
@Desc    : 重建 SKILL.md 的「## 规则文件索引」章节。

为什么需要它：
  `references/**` 下每新增/删除一个规则文件，SKILL.md 末尾那份「规则文件索引」就会和实际
  不一致（历史上已经陈旧过一次：索引里写着 `feature_impact_rules.md`、
  `merchant_common_rules.md`，而这两个文件根本不存在）。规则管理页上传/编辑规则后
  自动调本模块重建该章节，保证「模型看到的规则清单」与「实际会被注入的规则」一致。

⚠️ 两点必须清楚：
  ① 该章节**不参与规则匹配**。真正的匹配靠每个规则文件 front matter 里的
     `biz_module` / `page` 等值比对（见 plugin_loader），章节只是给人/模型看的说明；
  ② 本模块采用**整段替换**（用户明确要求「重建该节，不保留手写段落」），
     所以不要在该章节里手工写列表内容 —— 下次重建会被覆盖。
"""
import os
import re
import logging
from datetime import datetime

from app.core.config import config
from app.services.plugin_loader import parse_markdown_front_matter

logger = logging.getLogger("SKILL_INDEX_SYNC")

SECTION_TITLE = "## 规则文件索引"

# 模块 → 中文名（纯展示用；对不上就用 value 本身，不影响任何匹配逻辑）
MODULE_LABELS = {
    "bin": "卡BIN模块",
    "merchant": "商户模块",
    "secret-key": "密钥模块",
}


def _scan_reference_files(refs_dir: str):
    """扫描 references 下全部规则文件（跳过 _versions 备份目录），返回统一结构"""
    items = []
    if not os.path.isdir(refs_dir):
        return items
    for root, dirs, files in os.walk(refs_dir):
        dirs[:] = [d for d in dirs if d != "_versions"]
        for fn in sorted(files):
            if not fn.lower().endswith(".md"):
                continue
            path = os.path.join(root, fn)
            fm = parse_markdown_front_matter(path) or {}
            rel = os.path.relpath(path, refs_dir).replace("\\", "/")
            items.append({
                "rel": rel,
                "filename": fn,
                "biz_module": str(fm.get("biz_module", "") or "").strip(),
                "page": str(fm.get("page", "") or "").strip(),
                "rule_scope": str(fm.get("rule_scope", "module") or "module").strip(),
                "priority": int(fm.get("priority", 0) or 0),
                "description": str(fm.get("description", "") or "").strip(),
            })
    return items


def build_index_section(refs_dir: str) -> str:
    """按 references 的真实内容生成「规则文件索引」章节全文"""
    items = _scan_reference_files(refs_dir)

    common = [it for it in items if it["rule_scope"] == "common" or it["biz_module"] in ("", "all")]
    by_module = {}
    for it in items:
        if it in common:
            continue
        by_module.setdefault(it["biz_module"] or "未声明", []).append(it)

    lines = [
        SECTION_TITLE,
        "",
        "> 本节由「规则管理」页上传 / 编辑规则时自动重建（`utils/skill_index_sync.py`）。",
        "> **请勿手工维护本节的列表行** —— 下次重建会整段覆盖。",
        "> 注意：本节仅作说明，规则的**实际匹配**取决于每个文件 front matter 的 `biz_module` + `page`。",
        "",
    ]

    if common:
        lines.append("- 通用规则（`rule_scope: common` / `biz_module: all`，强制注入，不依赖语义召回）：")
        for it in common:
            lines.append(f"  - `{it['rel']}`" + (f" - {it['description']}" if it["description"] else ""))
    else:
        lines.append("- 通用规则：（当前无）")

    if by_module:
        for mod in sorted(by_module):
            label = MODULE_LABELS.get(mod, mod)
            lines.append(f"- {label}（`biz_module: {mod}`）：")
            for it in sorted(by_module[mod], key=lambda x: (x["page"], x["filename"])):
                tag = f"[page: {it['page']}]" if it["page"] else "[模块层：未声明 page]"
                desc = f" {it['description']}" if it["description"] else ""
                lines.append(f"  - `{it['rel']}` {tag}{desc}")
    else:
        lines.append("- 模块专属规则：（当前无）")

    return "\n".join(lines).rstrip() + "\n"


def rebuild_rule_index(skill_name: str = "req-test-cases") -> dict:
    """
    重建指定 skill 的 SKILL.md 「## 规则文件索引」章节（整段替换，幂等）。

    :return: {ok, changed, skill_md, section_chars, file_count, backup}
    """
    skill_dir = os.path.join(config.skills_dir, skill_name)
    refs_dir = os.path.join(skill_dir, "references")
    skill_md = os.path.join(skill_dir, "SKILL.md")

    if not os.path.exists(skill_md):
        logger.warning(f"⚠️ 索引同步跳过：SKILL.md 不存在 | {skill_md}")
        return {"ok": False, "message": f"SKILL.md 不存在: {skill_md}", "changed": False}

    with open(skill_md, "r", encoding="utf-8") as f:
        original = f.read()

    new_section = build_index_section(refs_dir)

    # 定位章节：从 `## 规则文件索引` 行起，到下一个 `## ` 标题（或文件尾）止
    m = re.search(r"^" + re.escape(SECTION_TITLE) + r"\s*$", original, re.M)
    if m:
        start = m.start()
        nxt = re.search(r"^##\s", original[m.end():], re.M)
        end = m.end() + nxt.start() if nxt else len(original)
        updated = original[:start] + new_section + ("\n" if nxt else "") + original[end:]
    else:
        # 章节不存在 → 追加到文件末尾（保持与既有约定一致：索引放最后）
        updated = original.rstrip() + "\n\n" + new_section

    if updated == original:
        logger.info(f"📒 SKILL.md 规则索引已是最新，无需改动 | skill={skill_name}")
        return {"ok": True, "changed": False, "skill_md": skill_md,
                "section_chars": len(new_section), "file_count": None}

    os.makedirs(refs_dir, exist_ok=True)
    backup = ""
    try:
        backup_dir = os.path.join(refs_dir, "_versions")
        os.makedirs(backup_dir, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d%H%M%S")
        backup = os.path.join(backup_dir, f"SKILL.md.{ts}.bak")
        with open(backup, "w", encoding="utf-8") as f:
            f.write(original)
    except Exception as e:
        logger.warning(f"⚠️ SKILL.md 备份失败（继续写入）: {str(e)}")

    with open(skill_md, "w", encoding="utf-8") as f:
        f.write(updated)

    file_count = len(_scan_reference_files(refs_dir))
    logger.info(
        f"📒 SKILL.md 规则索引已重建 | skill={skill_name} | 收录规则文件={file_count} | "
        f"章节 {len(new_section)} 字 | 备份={backup or '（无）'}"
    )
    return {"ok": True, "changed": True, "skill_md": skill_md,
            "section_chars": len(new_section), "file_count": file_count, "backup": backup}
