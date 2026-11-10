# -*- coding: utf-8 -*-
"""
@File    : metadata_manager.py
@Time    : 2026/7/9 13:45
@Author  : tanlei
"""
import os
import re
import json
import logging
import threading
from datetime import datetime
from typing import List, Dict, Any, Optional

logger = logging.getLogger("METADATA_MANAGER")

# 元数据文件是「读整个模块 → 加一个版本键 → 覆盖写」的读-改-写（RMW），
# 而 JSON 文件本身无事务 ⇒ 并发写会互相覆盖（丢版本）。此处按进程内串行化。
_MODULE_META_LOCK = threading.RLock()

# 用例状态枚举
CASE_STATUS = {
    "VALID": "valid",
    "MODIFY": "modify",
    "DEPRECATED": "deprecated",
    "NEW": "new"
}

# 用例表格默认表头（与 SKILL.md 输出规范保持一致）
# 实际解析以模型输出的真实表头为准，此处兜底
CASE_HEADERS = [
    "用例名称", "模块", "标签", "前置条件", "步骤描述",
    "预期结果", "编辑模式", "备注", "用例等级"
]

class MetadataManager:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._init_dirs()
        return cls._instance

    @classmethod
    def _init_dirs(cls):
        """初始化元数据存储目录"""
        cls.meta_root = os.path.join(os.getcwd(), "app", "metadata")
        cls.module_dir = os.path.join(cls.meta_root, "modules")
        cls.clarification_dir = os.path.join(cls.meta_root, "clarifications")
        os.makedirs(cls.module_dir, exist_ok=True)
        os.makedirs(cls.clarification_dir, exist_ok=True)

    def _get_module_file(self, module_name: str) -> str:
        """获取模块元数据文件路径（文件名安全处理）"""
        safe_name = re.sub(r'[^\w\-]', '_', module_name)
        return os.path.join(self.module_dir, f"{safe_name}_cases_meta.json")

    # ============ 版本键：必须带 page 维度 ============
    # 历史教训：早期用 version_{版本号} 作键（模块级），导致同一模块下不同页面互相覆盖，
    # 且增量召回时无法按 page 过滤 —— pool 页会命中「裁决页」的用例基线（跨页串味）。
    # 现改为 version::{page or 'all'}::{版本号}，page 为空表示模块层基线。
    _KEY_SEP = "::"

    @classmethod
    def _version_key(cls, page: str, req_version: str) -> str:
        """构造带 page 维度的版本键，如 version::pool::v1.0 / version::all::v1.0"""
        return f"version{cls._KEY_SEP}{(page or 'all')}{cls._KEY_SEP}{req_version}"

    @classmethod
    def _parse_version_key(cls, key: str):
        """解析版本键 → (page, version)；旧格式 version_v1.0 → ("", "v1.0")，非版本键返回 None"""
        if not key.startswith("version"):
            return None
        if cls._KEY_SEP in key:
            parts = key.split(cls._KEY_SEP)
            if len(parts) >= 3:
                page = parts[1]
                return ("" if page == "all" else page), parts[2]
            return None
        # 兼容旧格式 version_v1.0（无 page 维度）
        return "", key[len("version_"):]

    def parse_markdown_cases(self, md_content: str) -> List[Dict[str, Any]]:
        """
        核心方法：解析Markdown用例表格 → 结构化单条用例列表

        说明：复用 file_exporter 的统一解析逻辑，动态识别表头列数，
              避免因列数不匹配（如9列表格对8列常量）导致用例被整表丢弃。
        """
        cases = []
        try:
            from app.utils.file_exporter import parse_markdown_table
            headers, rows = parse_markdown_table(md_content)
        except Exception as e:
            logger.error(f"❌ 用例表格解析失败: {str(e)}")
            return cases

        if not rows:
            logger.warning("⚠️ 未解析到用例数据行，结构化元数据为空")
            return cases

        for row in rows:
            case = dict(zip(headers, row))
            # 补充默认字段
            case["case_status"] = CASE_STATUS["VALID"]
            case["create_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cases.append(case)

        logger.info(f"📊 用例结构化解析完成 | 列数={len(headers)} | 用例数={len(cases)}")
        return cases

    def save_module_cases(
        self,
        module_name: str,
        req_version: str,
        req_title: str,
        source_slug: str,
        md_content: str,
        case_list: List[Dict[str, Any]],
        page: str = ""
    ) -> str:
        """
        保存模块版本用例元数据（JSON文件持久化）

        :param page: 页面标识；"" = 模块层基线。同模块下不同页面各自独立成键，互不覆盖

        ⚠️ 两条硬约束（原实现违反后会造成**不可逆**数据丢失）：
        1. **读失败绝不能拿空 dict 覆盖写**。原实现 `except Exception: root_data = {}`，
           一次并发写产生半截 JSON 就足以让整个模块的历史版本 + 版本下拉全部消失；
           现在的做法是直接中止本次写入（宁可拒绝，也不能抹掉历史）。
        2. **写盘必须原子**（tmp + os.replace）。`open("w")` 中途崩溃会留下无法解析的 JSON，
           下一次读取又触发上面那条 ⇒ 索引整体清零。
        另外同模块的读-改-写全程持锁，避免并发沉淀互相覆盖。
        """
        file_path = self._get_module_file(module_name)

        with _MODULE_META_LOCK:
            root_data: Dict[str, Any] = {}

            # 读取已有历史版本
            if os.path.exists(file_path):
                try:
                    with open(file_path, "r", encoding="utf-8") as f:
                        root_data = json.load(f)
                    if not isinstance(root_data, dict):
                        raise ValueError("元数据根节点不是 JSON 对象")
                except Exception as e:
                    logger.error(
                        f"❌ 模块元数据读取/解析失败，本次写入已中止"
                        f"（宁可拒绝写入，也不能用空库覆盖掉历史版本）| file={file_path} | {str(e)}"
                    )
                    raise

            # 新版本数据
            # md_content 供增量迭代时作为历史基线召回，截断过短会导致合并后的用例缺失，
            # 因此放宽上限（原为3000字符，长用例集会被截掉大半）
            version_key = self._version_key(page, req_version)
            root_data[version_key] = {
                "module_name": module_name,
                "req_version": req_version,
                "req_title": req_title,
                "source_slug": source_slug,
                "page": (page or ""),
                "create_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "md_content": md_content[:50000],
                "case_total": len(case_list),
                "case_list": case_list
            }

            # 写入文件（原子替换：先写 .tmp，再 os.replace）
            tmp_path = file_path + ".tmp"
            try:
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(root_data, f, ensure_ascii=False, indent=2)
                os.replace(tmp_path, file_path)
            except Exception:
                # 清理半截临时文件，避免留垃圾
                try:
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
                except OSError:
                    pass
                raise

        logger.info(
            f"💾 模块版本用例元数据已保存 | module={module_name} | page={page or '模块层'} | "
            f"version={req_version} | 用例数={len(case_list)} | 键={version_key} | 文件={file_path}"
        )
        return file_path

    def get_module_version_cases(
            self,
            module_name: str,
            req_version: str,
            page: str = None
    ) -> Optional[Dict[str, Any]]:
        """
        查询指定模块 + 页面 + 版本的完整用例元数据

        :param page: 页面标识；None/"" = 模块层基线。
                     页面层查询时若只命中无 page 信息的旧数据，一律判为不可信并返回 None，
                     让调用方回退到带 page 过滤的向量检索，避免跨页串味。
        """
        file_path = self._get_module_file(module_name)
        if not os.path.exists(file_path):
            return None
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            target_page = (page or "").strip()

            # 1) 优先精确命中「本页」基线
            if target_page:
                record = data.get(self._version_key(target_page, req_version))
                if record:
                    return record
                logger.warning(
                    f"⚠️ 未命中页面基线 | module={module_name} | page={target_page} | "
                    f"version={req_version}（将回退向量检索）"
                )
                return None

            # 2) 模块层查询：先取模块层基线，再兜底旧格式（旧数据无 page 信息）
            record = data.get(self._version_key("", req_version))
            if record:
                return record
            legacy = data.get(f"version_{req_version}")
            if legacy:
                logger.info(
                    f"ℹ️ 命中旧格式模块基线（无 page 字段）| module={module_name} | version={req_version}"
                )
                return legacy
            return None
        except Exception:
            return None

    def get_module_all_versions(self, module_name: str) -> List[str]:
        """获取模块所有历史版本号列表（含新旧两种键格式，按版本号去重排序）"""
        file_path = self._get_module_file(module_name)
        if not os.path.exists(file_path):
            return []
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            versions = []
            for k in data.keys():
                parsed = self._parse_version_key(k)
                if parsed and parsed[1] not in versions:
                    versions.append(parsed[1])
            return sorted(versions)
        except Exception:
            return []

    def get_module_page_versions(self, module_name: str) -> List[Dict[str, str]]:
        """获取模块下「页面 + 版本」组合列表（供增量模式版本下拉展示，形如 pool / v1.0）"""
        file_path = self._get_module_file(module_name)
        if not os.path.exists(file_path):
            return []
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            items = []
            for k, v in data.items():
                parsed = self._parse_version_key(k)
                if not parsed:
                    continue
                page, version = parsed
                items.append({
                    "page": (v or {}).get("page", page),
                    "version": version,
                    "case_total": str((v or {}).get("case_total", "")),
                    "create_time": str((v or {}).get("create_time", "")),
                })
            return items
        except Exception:
            return []

    def get_next_version(self, module_name: str, page: str = "") -> str:
        """
        计算该 (module, page) 的下一个用例版本号（供增量沉淀使用，避免覆盖已有基线）。

        规则：取该页面已沉淀版本号中的最大值，补丁位 +1（v1.0 → v1.1；v2.3 → v2.4）；
        无历史版本或格式不可解析时返回 v1.0。
        """
        target_page = (page or "").strip()
        best = None
        for it in self.get_module_page_versions(module_name):
            if (it.get("page") or "") != target_page:
                continue
            m = re.match(r"^v?(\d+)\.(\d+)$", (it.get("version") or "").strip())
            if not m:
                continue
            pair = (int(m.group(1)), int(m.group(2)))
            if best is None or pair > best:
                best = pair
        return "v1.0" if best is None else f"v{best[0]}.{best[1] + 1}"

    # ============ 疑点澄清知识（增量影响面分析的持久化补充）============
    # 背景：影响面分析经常提出「业务规则不明确处」的疑点，用户当场答复后若不落库，
    #       下一次分析同一页面还会重复问一模一样的问题。此处按 (module, page) 持久化，
    #       并在下次分析时注入 prompt，形成「分析 → 澄清 → 复用」的闭环。
    @classmethod
    def _clarification_file(cls, module_name: str, page: str) -> str:
        """澄清知识文件路径：clarifications/{module}_{page}.json（page 为空记 all）"""
        safe_module = re.sub(r'[^\w\-]', '_', module_name or "unknown")
        safe_page = re.sub(r'[^\w\-]', '_', (page or "all") or "all")
        return os.path.join(cls.clarification_dir, f"{safe_module}_{safe_page}.json")

    def get_clarifications(self, module_name: str, page: str = "") -> List[Dict[str, str]]:
        """读取该 (module, page) 已沉淀的疑点澄清答复（按更新时间排序）"""
        file_path = self._clarification_file(module_name, page)
        if not os.path.exists(file_path):
            return []
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            items = data.get("items", []) if isinstance(data, dict) else []
            return items if isinstance(items, list) else []
        except Exception as e:
            logger.warning(f"⚠️ 读取澄清知识失败 | module={module_name} | page={page or '模块层'} | {str(e)}")
            return []

    def save_clarifications(
        self,
        module_name: str,
        page: str,
        items: List[Dict[str, str]]
    ) -> int:
        """
        保存疑点澄清答复（按疑点文本去重合并，同样的问题只保留最新答复）。

        :param items: [{"id": "Q1", "question": "...", "answer": "..."}]
        :return: 保存后的澄清条目总数
        """
        if not items:
            return len(self.get_clarifications(module_name, page))

        file_path = self._clarification_file(module_name, page)
        existing = self.get_clarifications(module_name, page)
        # 以疑点文本为键去重（编号可能漂移，文本更稳定）
        merged = {str(it.get("question", "")).strip(): it for it in existing if it.get("question")}
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for it in items:
            q = str(it.get("question", "")).strip()
            if not q:
                continue
            merged[q] = {
                "id": str(it.get("id", "")),
                "question": q,
                "answer": str(it.get("answer", "")).strip(),
                "update_time": now,
            }
        payload = {
            "module_name": module_name,
            "page": (page or ""),
            "items": list(merged.values()),
        }
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        logger.info(
            f"📝 疑点澄清知识已保存 | module={module_name} | page={page or '模块层'} | "
            f"本次提交={len(items)}条 | 累计={len(merged)}条 | 文件={file_path}"
        )
        return len(merged)


# 全局单例
meta_manager = MetadataManager()
