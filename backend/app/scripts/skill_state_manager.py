# -*- coding: utf-8 -*-
"""
@File    : skill_state_manager.py
@Time    : 2026/6/10 17:29
@Author  : tanlei
"""
import threading
import time
from typing import Dict, List, Optional

# 统一Skill状态枚举
SKILL_STATUS = {
    "pending": "待执行",
    "running": "执行中",
    "retrying": "重试中",
    "success": "执行成功",
    "failed": "执行失败",
    "timeout": "执行超时"
}

class SkillStateManager:
    _instance = None
    _lock = threading.Lock()

    def __init__(self):
        # 结构: {flow_id: {skill_name: 状态详情}}
        self.flow_states: Dict[str, Dict[str, dict]] = {}
        self.start_time_map: Dict[str, float] = {}  # 记录Skill开始执行时间

    @classmethod
    def get_instance(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = SkillStateManager()
        return cls._instance

    def init_flow(self, flow_id: str, skill_names: List[str]):
        """初始化一个流程下所有Skill的初始状态"""
        with self._lock:
            self.flow_states[flow_id] = {}
            for name in skill_names:
                self.flow_states[flow_id][name] = {
                    "skill_name": name,
                    "status": "pending",
                    "status_text": SKILL_STATUS["pending"],
                    "retry_count": 0,
                    "max_retry": 3,
                    "cost_time": 0.0,
                    "output": "",
                    "error_msg": ""
                }

    def update_skill_start(self, flow_id: str, skill_name: str):
        """更新Skill为【执行中】状态"""
        with self._lock:
            if flow_id not in self.flow_states:
                return
            state = self.flow_states[flow_id].get(skill_name)
            if state:
                state["status"] = "running"
                state["status_text"] = SKILL_STATUS["running"]
                self.start_time_map[f"{flow_id}_{skill_name}"] = time.time()

    def update_skill_retry(self, flow_id: str, skill_name: str, retry_cnt: int):
        """更新Skill为【重试中】状态"""
        with self._lock:
            if flow_id not in self.flow_states:
                return
            state = self.flow_states[flow_id].get(skill_name)
            if state:
                state["status"] = "retrying"
                state["status_text"] = SKILL_STATUS["retrying"]
                state["retry_count"] = retry_cnt

    def update_skill_success(self, flow_id: str, skill_name: str, output: str):
        """更新Skill为【执行成功】，记录耗时+中间结果"""
        with self._lock:
            if flow_id not in self.flow_states:
                return
            state = self.flow_states[flow_id].get(skill_name)
            if state:
                start_key = f"{flow_id}_{skill_name}"
                cost = round(time.time() - self.start_time_map.get(start_key, time.time()), 2)
                state["status"] = "success"
                state["status_text"] = SKILL_STATUS["success"]
                state["cost_time"] = cost
                state["output"] = output
                state["error_msg"] = ""

    def update_skill_failed(self, flow_id: str, skill_name: str, error_msg: str):
        """更新Skill为【执行失败】"""
        with self._lock:
            if flow_id not in self.flow_states:
                return
            state = self.flow_states[flow_id].get(skill_name)
            if state:
                start_key = f"{flow_id}_{skill_name}"
                cost = round(time.time() - self.start_time_map.get(start_key, time.time()), 2)
                state["status"] = "failed"
                state["status_text"] = SKILL_STATUS["failed"]
                state["cost_time"] = cost
                state["error_msg"] = error_msg

    def update_skill_timeout(self, flow_id: str, skill_name: str):
        """更新Skill为【超时】状态"""
        with self._lock:
            if flow_id not in self.flow_states:
                return
            state = self.flow_states[flow_id].get(skill_name)
            if state:
                start_key = f"{flow_id}_{skill_name}"
                cost = round(time.time() - self.start_time_map.get(start_key, time.time()), 2)
                state["status"] = "timeout"
                state["status_text"] = SKILL_STATUS["timeout"]
                state["cost_time"] = cost

    def get_flow_states(self, flow_id: str) -> Optional[dict]:
        """查询某个流程下所有Skill状态（给前端调用）"""
        with self._lock:
            return self.flow_states.get(flow_id)

    def clear_flow(self, flow_id: str):
        """清空单个流程状态"""
        with self._lock:
            if flow_id in self.flow_states:
                del self.flow_states[flow_id]

# 全局单例
skill_state_manager = SkillStateManager.get_instance()