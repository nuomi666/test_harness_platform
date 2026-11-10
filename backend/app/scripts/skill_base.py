# -*- coding: utf-8 -*-
"""
@File    : skill_base.py
@Time    : 2026/6/10 17:28
@Author  : tanlei
"""
import os
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from abc import ABC, abstractmethod
from typing import Dict, Any

from app.services.llm_adapter import LLMAdapter, LLMTruncationError


class BaseSkill(ABC):
    """Skill基类：统一超时控制 + 自动重试，无状态上报依赖"""

    def __init__(self, skill_name: str, priority: int = 0, timeout: int = 180, max_retry: int = 2):
        self.skill_name = skill_name
        self.priority = priority
        self.timeout = timeout  # 单次超时时间
        self.max_retry = max_retry  # 最大重试次数

    def set_flow_id(self, flow_id: str):
        """绑定当前流程ID"""
        self.flow_id = flow_id

    @abstractmethod
    def get_prompt(self, input_context: Dict[str, Any]) -> str:
        """子类实现：构造Prompt"""
        pass

    @abstractmethod
    def parse_output(self, raw_response: str) -> Dict[str, Any]:
        """子类实现：解析模型输出"""
        pass

    def load_skill_md(self) -> str:
        """读取当前skill同目录SKILL.md"""
        cls_file = os.path.abspath(self.__class__.__file__)
        skill_md_path = os.path.join(os.path.dirname(cls_file), "SKILL.md")
        if not os.path.exists(skill_md_path):
            raise FileNotFoundError(f"Skill[{self.skill_name}] SKILL.md不存在: {skill_md_path}")
        with open(skill_md_path, "r", encoding="utf-8") as f:
            return f.read()

    def _call_llm(self, prompt: str, thinking: str = None, max_tokens: int = None) -> str:
        """
        统一大模型调用入口

        :param thinking: 单次覆盖思考开关（enabled/disabled/auto）；
                         None=用 LLM_THINKING_TYPE 的默认值（默认 disabled）
        :param max_tokens: 单次覆盖输出上限；None=用 LLM_MAX_TOKENS
        """
        llm = LLMAdapter()
        content = llm.generate(prompt, thinking_type=thinking, max_tokens=max_tokens)
        return content
        # ============================================

    def _invoke_with_timeout(self, prompt: str) -> str:
        """
        单次 LLM 调用 + 超时保护（run / call_with_guard 共用，不重复实现两份）。

        ⚠️ 实现要点：**不能写成 `with ThreadPoolExecutor(...)`**。
        上下文管理器退出时会 `shutdown(wait=True)`，于是 `future.result()` 抛超时后
        仍会阻塞等那个 LLM 调用跑完 —— `self.timeout` 只是「延迟抛错」，
        调用方根本无法按时返回（前端 300s 超时放弃了，服务端还在占着 worker）。
        这里改为显式 `shutdown(wait=False)`：超时即刻上抛、不等待后台线程。
        """
        executor = ThreadPoolExecutor(max_workers=1)
        future = None
        try:
            future = executor.submit(self._call_llm, prompt)
            return future.result(timeout=self.timeout)
        except FutureTimeoutError:
            if future is not None:
                future.cancel()
            raise
        finally:
            executor.shutdown(wait=False)

    def _run_with_retry(self, prompt_factory) -> str:
        """
        统一的「超时 + 自动重试 + 截断直抛」循环。

        :param prompt_factory: 可调用对象，每次重试时重新构造 prompt
        （run 传 get_prompt 的包装，call_with_guard 直接返回现成 prompt）
        """
        retry_count = 0
        last_error = ""

        while retry_count <= self.max_retry:
            try:
                return self._invoke_with_timeout(prompt_factory())

            except FutureTimeoutError:
                last_error = f"执行超时（{self.timeout}s）"
                retry_count += 1
                print(f"⏱️ Skill[{self.skill_name}] {last_error}，第{retry_count}次重试")
                if retry_count > self.max_retry:
                    raise Exception(f"Skill[{self.skill_name}] {last_error}，已重试{self.max_retry}次")

            except LLMTruncationError:
                # 输出被截断（finish_reason=length 且正文为空）属**确定性失败**：
                # 相同入参重试结果不会改变，重试只会重复消耗时间与费用 ⇒ 直接抛出。
                raise

            except Exception as e:
                last_error = str(e)
                retry_count += 1
                traceback.print_exc()
                print(f"❌ Skill[{self.skill_name}] 执行失败：{last_error}，第{retry_count}次重试")
                if retry_count > self.max_retry:
                    raise Exception(f"Skill[{self.skill_name}] 执行失败：{last_error}")

        raise Exception(f"Skill[{self.skill_name}] 执行异常")

    def run(self, input_context: Dict[str, Any]) -> Dict[str, Any]:
        """统一执行入口：超时控制 + 自动重试"""
        raw_response = self._run_with_retry(lambda: self.get_prompt(input_context))
        parsed = self.parse_output(raw_response)
        print(f"✅ Skill[{self.skill_name}] 执行成功")
        return parsed

    def call_with_guard(self, prompt: str) -> str:
        """
        以**现成 prompt** 调用大模型，复用与 run() 完全相同的超时 / 重试 / 截断保护。

        用途：影响面分析、增量生成等「prompt 由 get_impact_prompt 这类专用方法构造、
        不经过 get_prompt(input_context)」的链路。
        历史问题：这些链路直接调 `self._call_llm(prompt)`，绕过了 run() 的超时与重试，
        使得 Skill 上声明的 timeout（如 45s）完全失效 —— LLM 挂起时同步 worker 被占满，
        前端 300s 超时放弃后服务端仍在跑。
        """
        return self._run_with_retry(lambda: prompt)

