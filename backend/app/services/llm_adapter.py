# -*- coding: utf-8 -*-
"""
@File    : llm_adapter.py
@Time    : 2026/5/25 11:49
@Author  : tanlei
"""
"""
统一大模型适配器
支持：通义千问、OpenAI GPT、字节豆包（OpenAI兼容模式）
切换模型 = 改配置，无需改业务逻辑
"""
"""
统一大模型适配器（修正版，适配火山方舟Doubao OpenAI兼容模式）
支持：通义千问、OpenAI GPT、火山方舟豆包（OpenAI兼容）
"""
import os
import logging
import dashscope
from dashscope import Generation
from openai import OpenAI


logger = logging.getLogger("LLM_ADAPTER")

# thinking.type 的合法取值（火山方舟：enabled 一定先思考；disabled 直接回答；auto 由模型自行判断）
VALID_THINKING_TYPES = ("enabled", "disabled", "auto")

# 环境变量里常见的开关写法 → thinking.type 归一化
_THINKING_ALIAS = {
    "off": "disabled", "false": "disabled", "0": "disabled", "none": "disabled", "no": "disabled",
    "on": "enabled", "true": "enabled", "1": "enabled", "yes": "enabled",
}


class LLMTruncationError(Exception):
    """模型输出被截断（finish_reason=length）且正文为空。

    这是**确定性失败**：同样的入参再来一次结果一样，重试只会浪费时间与费用。
    因此 BaseSkill.run 对它不做重试，直接向上抛出，让调用方显式感知
    （历史教训：空正文被当成正常返回，一路静默到前端表现为「没有变更点」）。
    """


class LLMAdapter:
    def __init__(self):
        self.model_type = os.getenv("LLM_TYPE", "dashscope")
        self.api_key = os.getenv("LLM_API_KEY")
        self.model = os.getenv("LLM_MODEL")
        self.temperature = float(os.getenv("LLM_TEMPERATURE", 0.3))
        # 输出 token 上限（默认 8192，可用环境变量 LLM_MAX_TOKENS 覆盖）。
        # 统一对 dashscope / openai / doubao 三个模型生效，防止默认上限导致输出截断。
        self.max_tokens = int(os.getenv("LLM_MAX_TOKENS", "8192"))
        # 思考开关（thinking.type）：思考型模型（doubao-seed-2.x / deepseek-v4 系列等）
        # **默认开启深度思考**，且 reasoning_tokens 计入 max_tokens —— 长 prompt 会让
        # 思考链把 8192 的输出预算吃光，正文一个字都输出不出来（finish_reason=length）。
        # 因此默认关闭思考；需要模型深度推理时可设 LLM_THINKING_TYPE=enabled/auto，
        # 或在单次调用时用 thinking_type 参数覆盖。取值非法/为空时不下发该参数（走平台默认）。
        self.thinking_type = (os.getenv("LLM_THINKING_TYPE", "disabled") or "").strip().lower()

    def _validate_config(self):
        """
        前置校验模型配置，避免把「空值 / 中文占位符」等非法配置直接送入 HTTP 层，
        否则 httpcore 编码请求头时会对非 ASCII 字符抛晦涩的
        “'ascii' codec can't encode characters” 错误，难排查。
        这里把它提前拦截，给出清晰可执行的中文提示。
        """
        def _is_placeholder(val: str) -> bool:
            """判断是否是 .env 里遗留的中文占位符（如『请填入你的火山方舟 API Key』）：
            - 空值
            - 含「请填入」标记
            - 含非 ASCII 字符（真实 API Key / 模型ID 均为纯 ASCII）
            其余一律视为合法配置，避免误杀正常 key。
            """
            if not val:
                return True
            if "请填入" in val:
                return True
            return not val.isascii()

        if not self.api_key or not self.api_key.strip() or _is_placeholder(self.api_key.strip()):
            raise Exception(
                "LLM_API_KEY 未配置或仍为占位符：请在 backend/.env 中填入火山方舟真实的 API Key"
                "（例如 LLM_API_KEY=xxxxxxxxxxxxxxxx，不要保留『请填入你的火山方舟 API Key』这串中文）"
            )
        if not self.model or not self.model.strip() or _is_placeholder(self.model.strip()):
            raise Exception(
                "LLM_MODEL 未配置或仍为占位符：请在 backend/.env 中填入火山方舟模型接入 ID"
                "（以 ep- 开头，如 LLM_MODEL=ep-202409xxxx-xxxx，不要保留中文示例文字）"
            )

    # ==================== 思考开关 ====================
    def _resolve_thinking(self, override: str = None) -> str:
        """
        归一化 thinking.type：
        - 返回 "enabled" / "disabled" / "auto" → 下发给平台
        - 返回 ""（空串）→ 不下发该参数，走平台默认（思考型模型=开启）
        """
        raw = (override if override is not None else self.thinking_type) or ""
        val = str(raw).strip().lower()
        if val in VALID_THINKING_TYPES:
            return val
        if val in _THINKING_ALIAS:
            return _THINKING_ALIAS[val]
        return ""

    # ==================== 用量与截断观测 ====================
    @staticmethod
    def _usage_dict(resp) -> dict:
        """兼容 pydantic / dict 两种 usage 结构，取 token 用量（含 reasoning_tokens）"""
        usage = getattr(resp, "usage", None)
        if usage is None:
            return {}
        try:
            data = usage.model_dump() if hasattr(usage, "model_dump") else dict(usage)
        except Exception:
            return {}
        details = data.get("completion_tokens_details") or {}
        return {
            "prompt_tokens": data.get("prompt_tokens"),
            "completion_tokens": data.get("completion_tokens"),
            "reasoning_tokens": details.get("reasoning_tokens"),
        }

    def _extract_openai_like(self, resp, limit: int, thinking: str) -> str:
        """
        统一处理 OpenAI 兼容响应：
        1. 记录 token 用量（可观测「思考链把预算吃光」这类问题）
        2. finish_reason=length 且正文为空 → 抛 LLMTruncationError（杜绝静默 0 字）
        3. finish_reason=length 但有正文 → 告警并返回（不丢已生成内容）
        """
        choice = resp.choices[0]
        msg = getattr(choice, "message", None)
        content = (getattr(msg, "content", None) or "") if msg is not None else ""
        finish = getattr(choice, "finish_reason", None)
        reasoning = (getattr(msg, "reasoning_content", None) or "") if msg is not None else ""
        usage = self._usage_dict(resp)

        logger.info(
            f"🤖 LLM 返回 | model={getattr(resp, 'model', self.model)} | "
            f"thinking={thinking or '平台默认'} | max_tokens={limit} | finish={finish} | "
            f"prompt_tokens={usage.get('prompt_tokens')} | completion_tokens={usage.get('completion_tokens')} | "
            f"reasoning_tokens={usage.get('reasoning_tokens')} | 正文={len(content)}字 | 思考={len(reasoning)}字"
        )

        if finish == "length":
            if not content.strip():
                raise LLMTruncationError(
                    f"模型输出被截断且正文为空：finish_reason=length，"
                    f"max_tokens={limit}（thinking={thinking or '平台默认'}，"
                    f"思考链={len(reasoning)}字/reasoning_tokens={usage.get('reasoning_tokens')}）。"
                    f"常见原因：思考型模型把输出预算全部用于思考链，或输出内容确实超过上限。"
                    f"处理建议：设 LLM_THINKING_TYPE=disabled 关闭思考，或减少单次输入/输出量（如改用分片）。"
                )
            logger.warning(
                f"⚠️ 模型输出被截断（finish_reason=length）但有正文内容 | "
                f"max_tokens={limit} | 正文={len(content)}字 | 建议检查是否需要在业务侧分片"
            )
        return content

    def generate(self, prompt: str, thinking_type: str = None, max_tokens: int = None) -> str:
        """
        统一生成接口，所有模型返回格式完全一致

        :param thinking_type: 单次覆盖思考开关（enabled/disabled/auto），None=用环境变量默认值
        :param max_tokens: 单次覆盖输出上限，None=用 LLM_MAX_TOKENS
        """
        self._validate_config()
        limit = int(max_tokens) if max_tokens else self.max_tokens
        thinking = self._resolve_thinking(thinking_type)
        if self.model_type == "dashscope":
            return self._call_dashscope(prompt, limit)
        elif self.model_type == "openai":
            return self._call_openai(prompt, limit, thinking)
        elif self.model_type == "doubao":
            # 火山方舟Doubao：用OpenAI兼容模式调用
            return self._call_doubao_openai_compat(prompt, limit, thinking)
        else:
            raise Exception(f"不支持的模型类型: {self.model_type}")

    # 1. 通义千问（原有逻辑不变）
    def _call_dashscope(self, prompt, limit=None):
        dashscope.api_key = self.api_key
        resp = Generation.call(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            temperature=self.temperature,
            max_tokens=limit or self.max_tokens
        )
        if resp.status_code != 200:
            raise Exception(f"通义千问调用失败：{resp.message}")
        return resp.output.choices[0].message.content

    # 2. OpenAI GPT（原有逻辑不变）
    def _call_openai(self, prompt, limit=None, thinking: str = ""):
        base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
        client = OpenAI(api_key=self.api_key, base_url=base_url)
        kwargs = {}
        if thinking:
            kwargs["extra_body"] = {"thinking": {"type": thinking}}
        resp = client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            temperature=self.temperature,
            max_tokens=limit or self.max_tokens,
            **kwargs
        )
        return self._extract_openai_like(resp, limit or self.max_tokens, thinking)

    # 3. 火山方舟 Doubao（OpenAI兼容模式，适配你当前配置）
    def _call_doubao_openai_compat(self, prompt, limit=None, thinking: str = ""):
        # 从环境变量读取火山方舟的Base URL
        base_url = os.getenv("DOUBAO_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3")
        client = OpenAI(api_key=self.api_key, base_url=base_url)
        limit = limit or self.max_tokens

        # thinking 通过 extra_body 透传（OpenAI SDK 的类型签名里没有该字段）
        kwargs = {}
        if thinking:
            kwargs["extra_body"] = {"thinking": {"type": thinking}}

        try:
            resp = client.chat.completions.create(
                model=self.model,  # 这里填你火山方舟的模型ID：Doubao-Seed-2.0-Code
                messages=[{"role": "user", "content": prompt}],
                temperature=self.temperature,
                max_tokens=limit,
                **kwargs
            )
            return self._extract_openai_like(resp, limit, thinking)
        except LLMTruncationError:
            # 截断是确定性失败，原样抛出（不要被下面的宽异常吞成「调用失败」）
            raise
        except UnicodeEncodeError as e:
            # 多半是 API Key / 模型ID / Base URL 仍为中文占位符或含非 ASCII 字符，
            # 被 httpcore 编码请求头时抛出的 ascii 编码错误
            raise Exception(
                "模型配置含非 ASCII 字符（多为 .env 的 LLM_API_KEY / LLM_MODEL 仍是中文占位符）。"
                "请在 backend/.env 填入火山方舟真实的 API Key 和模型接入 ID（纯 ASCII），"
                "例如 LLM_API_KEY=ep-xxx 对应 key、LLM_MODEL=ep-2024xxxx。原始错误："
                f"{str(e)}"
            ) from e
        except Exception as e:
            raise Exception(f"火山方舟豆包调用失败：{str(e)}")
