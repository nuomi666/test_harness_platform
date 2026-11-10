# -*- coding: utf-8 -*-
"""
@File    : context_manager.py
@Time    : 2026/5/26 17:52
@Author  : tanlei
"""
"""多流程隔离上下文管理器：支持多人同时操作，数据按flow_id完全隔离"""
from typing import Dict, Optional
import threading
import logging

logger = logging.getLogger("CONTEXT_MANAGER")


class GenerateContext:
    _instance = None
    _lock = threading.Lock()
    # 核心改造：从单流程上下文 → 按flow_id分组的多流程上下文
    _flow_contexts: Dict[str, dict] = {}

    @classmethod
    def get_instance(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = GenerateContext()
        return cls._instance

    def start_flow(self, flow_id: str, requirement: str, slug: str):
        """启动一个新流程，初始化独立上下文"""
        with self._lock:
            logger.info(f"🚀 启动新流程 | flow_id={flow_id} | slug={slug}")
            self._flow_contexts[flow_id] = {
                "flow_id": flow_id,
                "slug": slug,
                "requirement_text": requirement,
                "review_report": "",
                "test_cases": "",
                "current_stage": "init",
                "supplement_rules": [],
                "rag_context": {
                    "business": "",
                    "skill": ""
                }
            }

    def init_context(self, flow_id: str, slug: str, requirement: str = ""):
        """
        初始化流程上下文（兼容编排型Skill写入场景）
        已存在的流程不覆盖，仅补齐 slug，避免重复初始化丢失数据
        """
        with self._lock:
            if flow_id in self._flow_contexts:
                logger.debug(f"流程已存在，跳过初始化 | flow_id={flow_id}")
                return
            self._flow_contexts[flow_id] = {
                "flow_id": flow_id,
                "slug": slug,
                "requirement_text": requirement,
                "review_report": "",
                "test_cases": "",
                "current_stage": "init",
                "supplement_rules": [],
                "rag_context": {
                    "business": "",
                    "skill": ""
                }
            }
            logger.info(f"✅ 初始化流程上下文 | flow_id={flow_id} | slug={slug}")

    def update_context(self, flow_id: str, data: dict):
        """批量更新指定流程的上下文字段（不存在的流程自动初始化）"""
        with self._lock:
            if flow_id not in self._flow_contexts:
                self._flow_contexts[flow_id] = {
                    "flow_id": flow_id,
                    "slug": "",
                    "requirement_text": "",
                    "review_report": "",
                    "test_cases": "",
                    "current_stage": "init",
                    "supplement_rules": [],
                    "rag_context": {
                        "business": "",
                        "skill": ""
                    }
                }
            self._flow_contexts[flow_id].update(data)
            logger.debug(f"📝 更新流程上下文 | flow_id={flow_id} | 字段={list(data.keys())}")

    def find_by_slug(self, slug: str) -> Optional[str]:
        """
        按 slug 反查 flow_id（替代外部直接遍历 _flow_contexts 私有属性）
        :return: 命中的 flow_id，未命中返回 None
        """
        with self._lock:
            for fid, c in self._flow_contexts.items():
                if c.get("slug") == slug:
                    return fid
            return None

    def get_context_by_slug(self, slug: str) -> Optional[dict]:
        """按 slug 直接获取完整上下文（含 flow_id）"""
        with self._lock:
            for fid, c in self._flow_contexts.items():
                if c.get("slug") == slug:
                    ctx = c.copy()
                    ctx.setdefault("flow_id", fid)
                    return ctx
            return None

    def set_rag_context(self, flow_id: str, business: str, skill: str):
        """保存指定流程的RAG检索上下文"""
        with self._lock:
            if flow_id in self._flow_contexts:
                self._flow_contexts[flow_id]["rag_context"]["business"] = business
                self._flow_contexts[flow_id]["rag_context"]["skill"] = skill
                logger.debug(
                    f"🔍 保存RAG上下文 | flow_id={flow_id} | "
                    f"业务={len(business)}字 | 技能={len(skill)}字"
                )
            else:
                logger.warning(f"⚠️ 流程不存在，RAG上下文未保存 | flow_id={flow_id}")

    def set_review_report(self, flow_id: str, report: str):
        """保存指定流程的评审报告"""
        with self._lock:
            if flow_id in self._flow_contexts:
                self._flow_contexts[flow_id]["review_report"] = report
                self._flow_contexts[flow_id]["current_stage"] = "reviewed"
                logger.info(f"📄 保存评审报告 | flow_id={flow_id} | 长度={len(report)}字")
            else:
                logger.warning(f"⚠️ 流程不存在，评审报告未保存 | flow_id={flow_id}")

    def set_test_cases(self, flow_id: str, cases: str):
        """保存指定流程的测试用例"""
        with self._lock:
            if flow_id in self._flow_contexts:
                self._flow_contexts[flow_id]["test_cases"] = cases
                self._flow_contexts[flow_id]["current_stage"] = "completed"
                logger.info(f"🧪 保存测试用例 | flow_id={flow_id} | 长度={len(cases)}字")
            else:
                logger.warning(f"⚠️ 流程不存在，测试用例未保存 | flow_id={flow_id}")

    def add_supplement(self, flow_id: str, rule: str):
        """给指定流程追加补充业务规则"""
        with self._lock:
            if flow_id in self._flow_contexts:
                if rule and rule not in self._flow_contexts[flow_id]["supplement_rules"]:
                    self._flow_contexts[flow_id]["supplement_rules"].append(rule)
                    logger.info(f"➕ 追加补充规则 | flow_id={flow_id} | 内容={rule[:50]}")
            else:
                logger.warning(f"⚠️ 流程不存在，补充规则未保存 | flow_id={flow_id}")

    def get_context(self, flow_id: str) -> Optional[dict]:
        """获取指定流程的完整上下文"""
        with self._lock:
            ctx = self._flow_contexts.get(flow_id)
            if ctx is None:
                logger.debug(f"🔎 查询流程上下文未命中 | flow_id={flow_id}")
            return ctx.copy() if ctx else None

    def clear_flow(self, flow_id: str):
        """清空单个流程的上下文（流程结束可调用释放内存）"""
        with self._lock:
            if flow_id in self._flow_contexts:
                del self._flow_contexts[flow_id]
                logger.info(f"🗑️ 清理流程上下文 | flow_id={flow_id}")

    # def clear_all(self):
    #     """清空所有流程上下文"""
    #     self._flow_contexts.clear()


# 全局单例
context_manager = GenerateContext.get_instance()
