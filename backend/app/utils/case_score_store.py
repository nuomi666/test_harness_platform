# -*- coding: utf-8 -*-
"""
@File    : case_score_store.py
@Time    : 2026/9/29 14:30
@Author  : tanlei

用例质量评分的旁挂存储（sidecar）。

为什么走 sidecar 而不是给 9 列用例表加列：
  加列会破坏 file_exporter / metadata_manager / case_diff_analyzer 三处已同步的 9 列契约，
  所以质量分存旁挂文件 metadata/modules/{module}_case_scores.json，
  键与 metadata_manager 的版本键完全同构（version::{page or 'all'}::{version}）。

写盘纪律与 metadata_manager.save_module_cases 一致（同一类事故不能犯第二次）：
  ① 读失败直接中止，绝不用空 dict 覆盖历史评分；
  ② 原子写（tmp + os.replace）；
  ③ 同模块读-改-写全程持 _MODULE_META_LOCK。
"""
import os
import re
import json
import logging
from datetime import datetime
from typing import Dict, Any, Optional

from app.utils.metadata_manager import MetadataManager, meta_manager, _MODULE_META_LOCK

logger = logging.getLogger("CASE_SCORE_STORE")

# rubric 版本：改 scoring_rubric.md 的维度/权重/锚点时递增，供跨批次分数可比性判断
RUBRIC_VERSION = "1.0"


class CaseScoreStore:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def _get_module_file(self, module_name: str) -> str:
        """评分 sidecar 文件路径（与用例元数据同目录，文件名安全处理）"""
        safe_name = re.sub(r'[^\w\-]', '_', module_name or "unknown")
        return os.path.join(meta_manager.module_dir, f"{safe_name}_case_scores.json")

    # ============ 写入 ============
    def save_scores(
        self,
        module_name: str,
        page: str,
        req_version: str,
        cases: Dict[str, Any],
        engine_b: bool,
        source_slug: str = "",
        report_file: str = "",
        rubric_version: str = RUBRIC_VERSION,
    ) -> str:
        """
        保存一次评审的评分结果（按版本键覆盖该版本的旧评分——重评即刷新）。

        :param cases: {norm_用例名: {"name", "total", "dims", "hard_issues", "automatability",
                                     "fixture_pattern", "decision", "reason"}}
        :param engine_b: 本次是否启用引擎 B（LLM 打分）；False=纯代码硬指标模式
        """
        file_path = self._get_module_file(module_name)
        version_key = MetadataManager._version_key(page, req_version)

        with _MODULE_META_LOCK:
            root_data: Dict[str, Any] = {}
            if os.path.exists(file_path):
                try:
                    with open(file_path, "r", encoding="utf-8") as f:
                        root_data = json.load(f)
                    if not isinstance(root_data, dict):
                        raise ValueError("评分文件根节点不是 JSON 对象")
                except Exception as e:
                    logger.error(
                        f"❌ 用例评分文件读取/解析失败，本次写入已中止"
                        f"（宁可拒绝写入，也不能用空库覆盖历史评分）| file={file_path} | {str(e)}"
                    )
                    raise

            root_data[version_key] = {
                "module_name": module_name,
                "page": (page or ""),
                "req_version": req_version,
                "scored_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "rubric_version": rubric_version,
                "engine_b": bool(engine_b),
                "source_slug": source_slug,
                "report_file": report_file,
                "case_total": len(cases),
                "cases": cases,
            }

            tmp_path = file_path + ".tmp"
            try:
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(root_data, f, ensure_ascii=False, indent=2)
                os.replace(tmp_path, file_path)
            except Exception:
                try:
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
                except OSError:
                    pass
                raise

        logger.info(
            f"💾 用例评分已保存 | module={module_name} | page={page or '模块层'} | "
            f"version={req_version} | 用例数={len(cases)} | 引擎B={'开' if engine_b else '关'} | "
            f"键={version_key}"
        )
        return file_path

    # ============ 读取 ============
    def get_scores(self, module_name: str, page: str = "", req_version: str = "") -> Optional[Dict[str, Any]]:
        """
        查询指定 (module, page, version) 的评分记录；不存在/损坏返回 None（调用方自行降级）。
        req_version 为空时返回该模块的全部版本评分 {version_key: record}。
        """
        file_path = self._get_module_file(module_name)
        if not os.path.exists(file_path):
            return None
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return None
            if not req_version:
                return data or None
            return data.get(MetadataManager._version_key(page, req_version))
        except Exception as e:
            logger.warning(f"⚠️ 用例评分读取失败（按缺失处理）| module={module_name} | {str(e)}")
            return None

    # ============ 删留决策回写 ============
    def update_decisions(
        self,
        module_name: str,
        page: str,
        req_version: str,
        decisions: Dict[str, Dict[str, str]],
    ) -> int:
        """
        把人工确认后的删留决策回写到对应版本评分里（decision / reason / decided_at）。

        :param decisions: {norm_用例名: {"decision": "deleted"/"keep"/"improve", "reason": "..."}}
        :return: 实际更新的用例数
        """
        file_path = self._get_module_file(module_name)
        if not os.path.exists(file_path):
            logger.warning(f"⚠️ 评分文件不存在，跳过决策回写 | module={module_name}")
            return 0
        version_key = MetadataManager._version_key(page, req_version)
        updated = 0

        with _MODULE_META_LOCK:
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    root_data = json.load(f)
                if not isinstance(root_data, dict):
                    raise ValueError("评分文件根节点不是 JSON 对象")
            except Exception as e:
                logger.error(f"❌ 用例评分读取失败，决策回写中止 | file={file_path} | {str(e)}")
                return 0

            record = root_data.get(version_key)
            if not record:
                logger.warning(f"⚠️ 未命中版本评分，跳过决策回写 | 键={version_key}")
                return 0

            cases = record.get("cases") or {}
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            for norm_name, dec in (decisions or {}).items():
                entry = cases.get(norm_name)
                if not entry:
                    continue
                entry["decision"] = str(dec.get("decision", entry.get("decision", "")))
                if dec.get("reason"):
                    entry["reason"] = str(dec["reason"])
                entry["decided_at"] = now
                updated += 1

            tmp_path = file_path + ".tmp"
            try:
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(root_data, f, ensure_ascii=False, indent=2)
                os.replace(tmp_path, file_path)
            except Exception as e:
                logger.error(f"❌ 决策回写落盘失败 | file={file_path} | {str(e)}")
                try:
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
                except OSError:
                    pass
                return 0

        logger.info(
            f"📝 删留决策已回写 | module={module_name} | page={page or '模块层'} | "
            f"version={req_version} | 更新={updated}条"
        )
        return updated


# 全局单例
case_score_store = CaseScoreStore()
