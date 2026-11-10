# -*- coding: utf-8 -*-
"""
@File    : rule_file_manager.py
@Time    : 2026/9/29
@Desc    : 业务规则文件（= 「业务 skill」里的页面规则）的浏览 / 上传 / 替换 / 编辑。

概念对齐（很重要）：
  本框架里 **「上传业务 skill」= 往 `skills/req-test-cases/references/` 放页面规则文件**。
  skill 本体（req-test-cases）由流程代码写死，真正按 (模块, 页面) 注入的是 references 下的
  规则文件；因此上传一个「完整的新 skill 目录」不会被自动启用，也是没有意义的。

判定「同一页面的规则是否已存在」的依据 = **front matter 的 `biz_module` + `page` 等值匹配**
（与生成时注入规则的判据完全一致），**不看文件名、不看目录名**
（plugin_loader 用 rglob("*.md") 递归扫描，目录名不参与匹配）。

目录选择规则（用户确认）：
  · 该 module 已有规则文件 → **沿用**这些文件所在目录（如 bin 一直用 card-bin/）
  · 该 module 完全没有规则文件 → 新建 `references/{biz_module}/`（如 merchant/、secret-key/）

落盘安全：覆盖前必备份到 `references/_versions/{文件名}.{时间戳}.bak`
（`.bak` 不会被 plugin_loader 的 *.md 扫描命中，与规则补丁同一套约定）。
"""
import os
import re
import shutil
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import yaml

from app.core.config import config
from app.services.plugin_loader import parse_markdown_front_matter, load_all_skills
from app.utils.rule_patch_applier import rule_patch_applier
from app.utils.skill_index_sync import rebuild_rule_index

logger = logging.getLogger("RULE_FILE_MANAGER")

FRONT_MATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
# front matter 统一输出顺序（与既有规则文件保持一致，便于人眼比对）
FM_ORDER = ["description", "biz_module", "rule_scope", "priority", "page"]

# 业务 skill（页面规则）骨架模板：放 metadata/modules 下，供「规则管理」页下载。
# 注意用 __file__ 推导（不依赖 os.getcwd()），避免从别的工作目录启动时找不到模板。
_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE_FILENAME = "业务skill模板.md"
TEMPLATE_DIR = os.path.join(_APP_DIR, "metadata", "modules")


class RuleFileManager:
    def __init__(self, skill_name: str = "req-test-cases"):
        self.skill_name = skill_name
        self.skill_dir = os.path.join(config.skills_dir, skill_name)
        self.refs_dir = os.path.join(self.skill_dir, "references")
        self.backup_dir = os.path.join(self.refs_dir, "_versions")

    # ==================== 0. 业务 skill 骨架模板（下载用）====================
    @staticmethod
    def template_path() -> str:
        """模板文件绝对路径：优先 app/metadata/modules/，兜底 meta_manager 的解析结果"""
        primary = os.path.join(TEMPLATE_DIR, TEMPLATE_FILENAME)
        if os.path.exists(primary):
            return primary
        try:
            from app.utils.metadata_manager import meta_manager
            alt = os.path.join(meta_manager.module_dir, TEMPLATE_FILENAME)
            if os.path.exists(alt):
                return alt
        except Exception as e:      # 只在兜底路径上失败，不影响主路径
            logger.warning(f"⚠️ 模板兜底路径解析失败 | {e}")
        return primary

    def get_template(self) -> Dict[str, Any]:
        """
        读取业务 skill 骨架模板全文（规则管理页「下载业务规则模板」用）。

        只读下发，不做任何 front matter 补正 —— 模板里的 biz_module/page 是占位符，
        使用者必须自己替换；若直接原样上传，预检弹窗会提示归属不一致。
        """
        path = self.template_path()
        if not os.path.exists(path):
            logger.error(f"❌ 业务规则模板缺失 | 期望路径={path}")
            return {"ok": False, "message": f"模板文件不存在：{path}",
                    "filename": TEMPLATE_FILENAME, "content": ""}
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        logger.info(f"📄 下发业务规则模板 | 文件={path} | 字数={len(content)}")
        return {"ok": True, "filename": TEMPLATE_FILENAME, "path": path,
                "content": content, "char_count": len(content)}

    # ==================== 1. 目录与文件名 ====================
    @staticmethod
    def _sanitize_filename(name: str, page: str = "") -> str:
        """文件名只允许 [\\w.-]，且必须以 .md 结尾；不合法时按 page 兜底"""
        n = (name or "").strip().replace("\\", "/").split("/")[-1]
        n = re.sub(r"[^\w.\-]", "_", n, flags=re.UNICODE)
        if not n or not n.lower().endswith(".md"):
            base = re.sub(r"[^\w\-]", "_", (page or "module").strip()) or "module"
            n = f"{base}_page_rules.md" if page else f"{base}_module_rules.md"
        if ".." in n or n.startswith("."):
            n = "rule_" + n.lstrip(".")
        return n

    def module_dir(self, module: str) -> Tuple[str, bool]:
        """
        该 module 的规则文件应放哪个目录。

        :return: (目录绝对路径, 是否新建目录)
        """
        module = (module or "").strip()
        existing = [f for f in self._scan_files(ignore_module_filter=True)
                    if f["biz_module"] == module]
        if existing:
            # 沿用已有目录（取相对路径排序后的第一个，保证确定性）
            first = sorted(existing, key=lambda x: x["rel"])[0]
            return os.path.dirname(first["path"]), False
        return os.path.join(self.refs_dir, module or "common"), True

    # ==================== 2. 扫描与定位 ====================
    def _scan_files(self, ignore_module_filter: bool = False) -> List[Dict[str, Any]]:
        items = []
        if not os.path.isdir(self.refs_dir):
            return items
        for root, dirs, files in os.walk(self.refs_dir):
            dirs[:] = [d for d in dirs if d != "_versions"]
            for fn in sorted(files):
                if not fn.lower().endswith(".md"):
                    continue
                path = os.path.join(root, fn)
                fm = parse_markdown_front_matter(path) or {}
                items.append({
                    "path": path,
                    "filename": fn,
                    "rel": os.path.relpath(path, self.refs_dir).replace("\\", "/"),
                    "dir": os.path.dirname(path),
                    "biz_module": str(fm.get("biz_module", "") or "").strip(),
                    "page": str(fm.get("page", "") or "").strip(),
                    "rule_scope": str(fm.get("rule_scope", "module") or "module").strip(),
                    "priority": int(fm.get("priority", 0) or 0),
                    "description": str(fm.get("description", "") or "").strip(),
                    # 「需求变更改动」标注（由规则补丁落盘时写入 front matter，见 rule_patch_applier）
                    "last_change_origin": str(fm.get("last_change_origin", "") or "").strip(),
                    "last_change_at": str(fm.get("last_change_at", "") or "").strip(),
                    "last_change_note": str(fm.get("last_change_note", "") or "").strip(),
                    "changed_rule_ids": list(fm.get("changed_rule_ids") or []),
                })
        return items

    def list_files(self, module: str = "", page: str = "") -> Dict[str, Any]:
        """
        列出规则文件。两种口径：
          · 传了 module → 该 (module, page) **精确命中**的文件（与生成时注入的判据一致）
          · module 为空   → 列出 references 下全部文件（规则管理页的「全部规则」视图）
        额外回传「该模块下所有文件」与「同模块但 page 不匹配」的项，便于前端提示
        「规则存在但不在当前页面」这类静默不一致。
        """
        module = (module or "").strip()
        page = (page or "").strip()
        all_files = self._scan_files()

        def enrich(it: Dict[str, Any]) -> Dict[str, Any]:
            try:
                with open(it["path"], "r", encoding="utf-8") as fp:
                    text = fp.read()
                rows = rule_patch_applier.parse_rule_rows(text)
                ids = [r["id"] for r in rows]
            except Exception as e:
                logger.warning(f"⚠️ 读取规则文件失败 | {it['filename']} | {str(e)}")
                text, ids = "", []
            out = dict(it)
            out.update({"char_count": len(text), "rule_ids": ids, "rule_count": len(ids)})
            return out

        if not module:
            return {"code": 0, "module": "", "page": "",
                    "data": [enrich(it) for it in sorted(all_files, key=lambda x: x["rel"])],
                    "mode": "all"}

        exact = [it for it in all_files if it["biz_module"] == module and it["page"] == page]
        same_module = [it for it in all_files if it["biz_module"] == module and it["page"] != page]
        return {
            "code": 0, "module": module, "page": page, "mode": "scope",
            "data": [enrich(it) for it in sorted(exact, key=lambda x: x["rel"])],
            "same_module_files": [
                {"filename": it["filename"], "rel": it["rel"], "page": it["page"],
                 "description": it["description"]}
                for it in sorted(same_module, key=lambda x: x["rel"])
            ],
            "hint": ("该页面还没有专属规则文件；生成时会只用通用规则 + 语义兜底"
                     "（不报错，但页面级精确注入为空）。可在本页上传一份。") if not exact else "",
        }

    def read_file(self, filename: str, module: str = "", page: str = "") -> Dict[str, Any]:
        """按文件名（或 module+page 命中）读取规则文件全文"""
        target = self._locate_one(filename, module, page)
        if not target:
            return {"ok": False, "message": f"未找到规则文件: {filename or (module + '/' + page)}"}
        with open(target["path"], "r", encoding="utf-8") as f:
            content = f.read()
        rows = rule_patch_applier.parse_rule_rows(content)
        return {"ok": True, "data": {
            "filename": target["filename"], "rel": target["rel"], "path": target["path"],
            "biz_module": target["biz_module"], "page": target["page"],
            "rule_scope": target["rule_scope"], "description": target["description"],
            "char_count": len(content), "rule_ids": [r["id"] for r in rows],
            "front_matter": parse_markdown_front_matter(target["path"]) or {},
            "content": content,
        }}

    def _locate_one(self, filename: str, module: str = "", page: str = "") -> Optional[Dict[str, Any]]:
        all_files = self._scan_files()
        if filename:
            safe = self._sanitize_filename(filename, page)
            for it in all_files:
                if it["filename"] == safe or it["rel"] == (filename or "").replace("\\", "/"):
                    return it
            return None
        if module:
            hits = [it for it in all_files if it["biz_module"] == module and it["page"] == (page or "").strip()]
            return sorted(hits, key=lambda x: x["rel"])[0] if hits else None
        return None

    # ==================== 3. 预检（前端弹确认框用）====================
    def plan_target(self, module: str, page: str = "", filename: str = "",
                    content: str = "", description: str = "") -> Dict[str, Any]:
        """
        落盘前的预检：命中哪个文件 / 会新建到哪个目录 / front matter 有没有冲突。

        front matter 冲突（文件里写的 biz_module/page 与所选作用域不一致）**不静默改写**：
        这里只把差异报出来，由前端弹窗让用户二选一，用户的选择通过 front_matter_mode 传回。
        """
        module = (module or "").strip()
        page = (page or "").strip()
        if not module:
            return {"ok": False, "message": "必须选择业务模块（module 是规则归属的锚点）"}

        hits = [it for it in self._scan_files()
                if it["biz_module"] == module and it["page"] == page]
        target_dir, is_new_dir = self.module_dir(module)
        fname = self._sanitize_filename(filename, page)
        target_path = hits[0]["path"] if hits else os.path.join(target_dir, fname)

        fm_before, body = self._split_front_matter(content or "")
        conflicts = []
        if content:
            fm_mod = str(fm_before.get("biz_module", "") or "").strip()
            fm_page = str(fm_before.get("page", "") or "").strip()
            if fm_mod != module:
                conflicts.append({"field": "biz_module", "file": fm_mod or "（缺失）", "selected": module})
            if fm_page != page:
                conflicts.append({"field": "page", "file": fm_page or "（缺失）", "selected": page})

        return {
            "ok": True,
            "action": "replace" if hits else "create",
            "module": module, "page": page,
            "target_path": target_path,
            "target_rel": os.path.relpath(target_path, self.refs_dir).replace("\\", "/"),
            "target_dir": os.path.relpath(target_dir, self.refs_dir).replace("\\", "/"),
            "new_dir": is_new_dir,
            "filename": os.path.basename(target_path),
            "existing": [{"filename": it["filename"], "rel": it["rel"],
                          "char_count": os.path.getsize(it["path"])} for it in hits],
            "conflicts": conflicts,
            "will_override_front_matter": bool(conflicts),
            "note": ("该作用域下已有规则文件，保存会**覆盖**它（旧文件先备份到 "
                     "references/_versions/）" if hits
                     else f"该作用域下暂无规则文件，将新建到 references/{os.path.relpath(target_dir, self.refs_dir).replace(chr(92), '/')}/"),
        }

    # ==================== 4. front matter 处理 ====================
    @staticmethod
    def _split_front_matter(content: str) -> Tuple[Dict[str, Any], str]:
        m = FRONT_MATTER_RE.match(content or "")
        if not m:
            return {}, content or ""
        try:
            fm = yaml.safe_load(m.group(1)) or {}
            if not isinstance(fm, dict):
                fm = {}
        except Exception:
            fm = {}
        return fm, content[m.end():]

    @staticmethod
    def _render_front_matter(fm: Dict[str, Any]) -> str:
        """按 FM_ORDER 稳定输出 front matter（其余键追加在后），中文不转义"""
        ordered = {}
        for k in FM_ORDER:
            if k in fm and fm[k] not in (None, ""):
                ordered[k] = fm[k]
        for k, v in fm.items():
            if k not in ordered and v not in (None, ""):
                ordered[k] = v
        body = yaml.safe_dump(ordered, allow_unicode=True, sort_keys=False, default_flow_style=False)
        return "---\n" + body + "---\n"

    def ensure_front_matter(self, content: str, module: str, page: str,
                            mode: str = "override", description: str = "") -> Tuple[str, Dict, Dict]:
        """
        按用户选择处理 front matter。

        :param mode: "override" = 以页面选择为准自动补正（用户确认的默认项）；
                     "keep"     = 原样保留文件里写的
        :return: (新内容, 修正前 fm, 修正后 fm)
        """
        fm, body = self._split_front_matter(content)
        before = dict(fm)
        if mode == "keep":
            return content, before, dict(fm)

        fm["biz_module"] = module
        if page:
            fm["page"] = page
        else:
            fm.pop("page", None)          # 页面留空 = 模块层，不写 page 键
        if description:
            fm["description"] = description
        fm.setdefault("rule_scope", "module")
        after = dict(fm)
        return self._render_front_matter(fm) + body.lstrip("\n"), before, after

    # ==================== 5. 落盘 ====================
    def save_rule_file(self, module: str, page: str = "", filename: str = "",
                       content: str = "", front_matter_mode: str = "override",
                       description: str = "", sync_index: bool = True,
                       reload_skills: bool = True, target_rel: str = "") -> Dict[str, Any]:
        """
        保存（新建或覆盖）规则文件：front matter 处理 → 备份 → 写盘 → 重建索引 → 热重载。

        :param target_rel: **仅用于「编辑已有文件」**——直接指定要覆盖的 references 内相对路径。
            为什么需要它：正常定位靠 front matter 的 (biz_module, page) 等值匹配，
            但一个 front matter 缺失/写错的既有文件根本匹配不上，走常规路径会**新建**一份
            而不是覆盖它（用户以为在改，实际多了个文件）。编辑流程带着读回来的 rel 精确回写，
            避免这个静默分叉。路径必须落在 references/ 之内（越界直接拒绝）。
        """
        module = (module or "").strip()
        page = (page or "").strip()
        target_rel = (target_rel or "").strip().replace("\\", "/")
        if not module and not target_rel:
            return {"ok": False, "message": "必须选择业务模块"}
        if not (content or "").strip():
            return {"ok": False, "message": "规则内容不能为空"}

        new_content, fm_before, fm_after = self.ensure_front_matter(
            content, module, page, front_matter_mode, description)

        explicit_path = ""
        if target_rel:
            base = os.path.realpath(self.refs_dir)
            cand = os.path.realpath(os.path.join(self.refs_dir, target_rel))
            if not (cand == base or cand.startswith(base + os.sep)):
                return {"ok": False, "message": f"目标路径越界: {target_rel}"}
            if not os.path.isfile(cand):
                return {"ok": False, "message": f"要编辑的规则文件不存在: {target_rel}"}
            explicit_path = cand

        if explicit_path:
            target = explicit_path
            action = "replace"
        else:
            hits = [it for it in self._scan_files()
                    if it["biz_module"] == module and it["page"] == page]
            if hits:
                target = sorted(hits, key=lambda x: x["rel"])[0]["path"]
                action = "replace"
            else:
                target_dir, is_new_dir = self.module_dir(module)
                os.makedirs(target_dir, exist_ok=True)
                target = os.path.join(target_dir, self._sanitize_filename(filename, page))
                action = "create"

        # 越界校验：目标必须落在 references/ 之内
        base = os.path.realpath(self.refs_dir)
        real = os.path.realpath(target)
        if not (real == base or real.startswith(base + os.sep)):
            return {"ok": False, "message": f"目标路径越界: {target}"}

        # 备份（同规则补丁约定：.bak 不会被 plugin_loader 扫描命中）
        backup = ""
        if os.path.exists(real):
            try:
                os.makedirs(self.backup_dir, exist_ok=True)
                ts = datetime.now().strftime("%Y%m%d%H%M%S")
                backup = os.path.join(self.backup_dir, f"{os.path.basename(real)}.{ts}.bak")
                shutil.copy2(real, backup)
            except Exception as e:
                logger.warning(f"⚠️ 规则文件备份失败（继续写入）: {str(e)}")

        with open(real, "w", encoding="utf-8") as f:
            f.write(new_content)

        logger.info(
            f"📝 规则文件已落盘 | 动作={action} | module={module} | page={page or '模块层'} | "
            f"文件={os.path.relpath(real, self.refs_dir)} | {len(new_content)} 字 | 备份={backup or '（无）'}"
        )

        index_result = rebuild_rule_index(self.skill_name) if sync_index else {}
        reload_result = load_all_skills() if reload_skills else {}

        # ⚠️ keep 模式 + front matter 不匹配 = 文件会「归属到别的页面」：
        #    规则匹配靠 front matter 等值比对，所以这份文件不会被当前所选作用域注入，
        #    而且下次在同一个页面再上传还会「看不到它」→ 又新建一份（重复文件）。
        #    必须显式告警，否则这是一个完全静默的失效点。
        warning = ""
        after_mod = str(fm_after.get("biz_module", "") or "").strip()
        after_page = str(fm_after.get("page", "") or "").strip()
        # ⚠️ 注意：只比「不一致」不够 —— module/page 都是空串时两边相等，会漏报
        #    「文件压根没有 biz_module」这一最严重的静默失效（生成时任何页面都不会注入它）。
        #    所以必须显式判 not after_mod。
        if not after_mod:
            warning = (
                f"⚠️ 这份文件的 front matter 里没有 biz_module（page={after_page or '（空）'}）："
                f"**生成时不会被任何页面注入**，规则管理页也不会把它算进任何页面。"
                f"如需生效，请改用「以页面选择为准补正」重新保存一次。内容已按你的编辑结果写入。"
            )
            logger.warning(f"⚠️ 规则文件缺 biz_module | 文件={os.path.relpath(real, self.refs_dir)}")
        elif after_mod != module or after_page != page:
            warning = (
                f"⚠️ 文件 front matter 声明的是 biz_module={after_mod} / "
                f"page={after_page or '（空）'}，与所选作用域（{module or '（未选）'} / {page or '模块层'}）不一致："
                f"这份规则**不会被当前页面的生成流程注入**，且本页规则列表里也看不到它。"
                f"如需在当前页面生效，请改用「以页面选择为准补正」。"
            )
            logger.warning(f"⚠️ 规则归属与所选作用域不一致 | 文件={after_mod}/{after_page} | 所选={module}/{page}")

        return {
            "ok": True,
            "action": action,
            "message": ("已覆盖同页面规则文件" if action == "replace" else "已新建规则文件")
                       + "，并自动热重载知识库（下次生成即刻生效，无需重启后端）",
            "warning": warning,
            "path": real,
            "rel": os.path.relpath(real, self.refs_dir).replace("\\", "/"),
            "filename": os.path.basename(real),
            "backup": backup,
            "front_matter_before": fm_before,
            "front_matter_after": fm_after,
            "front_matter_mode": front_matter_mode,
            "index": index_result,
            "reloaded": bool(reload_result),
            "loaded_docs": (reload_result or {}).get("count", 0),
            "content": new_content,
        }


# 全局单例
rule_file_manager = RuleFileManager()
