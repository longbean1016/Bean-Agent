"""OpenAI 兼容的语言模型调用层。

统一处理普通回复、流式增量、工具调用、请求重试和 Prompt Cache 指标，
使 Agent 核心循环不依赖具体模型供应商的 SDK 返回结构。
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import os
import re
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from openai import AsyncOpenAI

from agent.config_models import LLMConfig, VisionConfig
from agent.context_budget import estimate_payload_tokens, estimate_tokens

logger = logging.getLogger(__name__)

# 请求快照可能包含完整对话和记忆，默认必须关闭，只允许受控调试显式开启。
_LLM_PAYLOAD_SNAPSHOT_ENABLED = False
_LAST_PAYLOAD_PATH = Path(tempfile.gettempdir()) / "beanagent-last-llm-payload.json"
_PAYLOAD_SNAPSHOT_DIR = Path(tempfile.gettempdir()) / "beanagent-llm-payloads"
_PAYLOAD_SNAPSHOT_SEQ = itertools.count(1)

StreamDelta = dict[str, str]
StreamCallback = Callable[[StreamDelta], Awaitable[None]]

# 不同 OpenAI 兼容服务返回的错误类型并不统一，因此上下文超限不能只看
# status_code。这里集中维护已知文本特征，后续 Pipeline 可据此触发历史裁剪。
_CONTEXT_LENGTH_KEYWORDS = (
    "range of input length",
    "context_length_exceeded",
    "maximum context length",
    "context window exceeds limit",
    "string too long",
    "reduce the length",
    "too many tokens",
)

# 只重试“相同请求稍后可能成功”的错误：限流、服务端故障和网关故障。
# 400/401/403 等请求或鉴权错误必须立即暴露，重试不会改变结果。
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
_RETRYABLE_KEYWORDS = (
    "429",
    "timeout",
    "timed out",
    "connect",
    "connection",
    "temporarily unavailable",
    "server error",
    "502",
    "503",
    "504",
    "rate limit",
    "too many requests",
)

_SAFETY_ERROR_CODES = {
    "data_inspection_failed",
    "content_filter",
    "content_policy_violation",
}

_THINK_RE = re.compile(r"<think>(.*?)</think>", re.DOTALL)


@dataclass
class ToolCall:
    """Provider 交给工具注册表执行的统一工具调用。"""

    id: str  # 本轮工具调用标识，工具结果需要用它与 assistant 消息关联
    name: str  # ToolRegistry 中注册的工具名称
    arguments: dict[str, Any]  # 模型返回的 JSON 参数，解析后再交给工具校验

@dataclass
class LLMResponse:
    """一次完整模型调用的归一化结果。"""

    content: str | None  # 最终文本；纯工具调用时通常为 None
    tool_calls: list[ToolCall] = field(default_factory=list)  # 待执行工具列表
    thinking: str | None = None  # 供应商返回的 reasoning_content
    cache_prompt_tokens: int | None = None  # 本次输入的总 Prompt token
    cache_hit_tokens: int | None = None  # 其中由 Prompt Cache 命中的 token
    # DeepSeek 工具续轮必须把 reasoning_content 原样放回 assistant 消息。
    # 该字典让未来 Pipeline 无需了解供应商字段，也不会把它混入用户正文。
    provider_fields: dict[str, Any] = field(default_factory=dict)


class ContextLengthError(Exception):
    """模型因输入超过上下文窗口而拒绝请求。"""


class ContentSafetyError(Exception):
    """模型供应商因内容安全策略拒绝请求。"""


class ProviderStrategy:
    """OpenAI 兼容 Provider 的基础策略。

    新供应商只需继承并覆盖存在差异的步骤，LLMProvider 的请求、重试和
    流式聚合无需修改。
    """

    def normalize_messages(
        self, messages: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """规范通用消息，并移除非 DeepSeek 请求中的 reasoning 字段。"""

        return _strip_reasoning_content(_normalize_chat_messages(messages))

    def prepare_request(
        self,
        request: dict[str, Any],
        extra_body: dict[str, Any],
        *,
        disable_thinking: bool,
    ) -> None:
        """把通用扩展参数加入请求。"""

        if disable_thinking:
            _drop_thinking_keys(extra_body)
        if extra_body:
            request["extra_body"] = extra_body

    def extract_message(
        self,
        message: Any,
        content: str | None,
    ) -> tuple[str | None, str | None, dict[str, Any]]:
        """兼容把推理内容包在 ``<think>`` 中的普通接口。"""

        thinking: str | None = None
        if content:
            match = _THINK_RE.search(content)
            if match:
                thinking = match.group(1).strip()
                content = _THINK_RE.sub("", content).strip() or None
        return content, thinking, {}

    def provider_fields_for_tool_call(
        self,
        fields: dict[str, Any],
        request: dict[str, Any],
    ) -> dict[str, Any]:
        """返回工具续轮需要保留的供应商字段。"""

        return fields

    def prepare_stream_request(
        self, request: dict[str, Any]
    ) -> dict[str, Any]:
        """创建通用流式请求。"""

        return {**request, "stream": True}


class DeepSeekStrategy(ProviderStrategy):
    """处理 DeepSeek 的请求与响应协议差异。

    Strategy 只处理 DeepSeek 特有差异；重试、流式消费和 Cache 指标仍由
    ``LLMProvider`` 统一负责。当前 BeanAgent 仅支持 DeepSeek，因此暂不
    引入其他供应商策略，也不增加无意义的策略选择分支。
    """

    def normalize_messages(
        self, messages: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """复制并规范消息，同时移除 DeepSeek 文本接口不支持的图片块。"""

        # 不为工具调用编造说明文本，只规范空 content；真实信息保留在 tool_calls。
        normalized = _normalize_chat_messages(
            messages, fill_tool_call_content=False
        )
        return _strip_image_url_blocks(normalized)

    def prepare_request(
        self,
        request: dict[str, Any],
        extra_body: dict[str, Any],
        *,
        disable_thinking: bool,
    ) -> None:
        """映射 thinking 配置，并原地补充最终 DeepSeek 请求参数。"""

        # enable_thinking 是配置层的友好别名，DeepSeek 实际接受的是
        # extra_body.thinking.type；pop 可避免把非协议字段继续发送给服务端。
        thinking_enabled = extra_body.pop("enable_thinking", None)
        reasoning_effort = extra_body.pop("reasoning_effort", None)
        thinking_requested = bool(thinking_enabled) or bool(reasoning_effort)
        if _deepseek_thinking_enabled(extra_body):
            thinking_requested = True

        # 单次禁用拥有最高优先级：覆盖全局 enabled，同时禁止发送推理强度。
        if disable_thinking:
            extra_body["thinking"] = {"type": "disabled"}
            reasoning_effort = None
            thinking_requested = False
        elif thinking_enabled is not None and "thinking" not in extra_body:
            extra_body["thinking"] = {
                "type": "enabled" if bool(thinking_enabled) else "disabled"
            }
            thinking_requested = bool(thinking_enabled)

        # 将通用等级 xhigh 映射为 DeepSeek 接受的 max；thinking 被
        # 明确关闭时不能继续发送 reasoning_effort，否则请求语义互相冲突。
        if reasoning_effort and not _deepseek_thinking_disabled(extra_body):
            request["reasoning_effort"] = _normalize_deepseek_effort(
                str(reasoning_effort)
            )

        # Thinking 多轮协议要求 assistant 历史含 reasoning_content。只在
        # 确实请求 thinking 时修复，且每条消息都复制，绝不污染 Session 原数据。
        if thinking_requested and not _deepseek_thinking_disabled(extra_body):
            messages = request.get("messages")
            if isinstance(messages, list):
                request["messages"] = _ensure_deepseek_reasoning_content(messages)

        if extra_body:
            request["extra_body"] = extra_body

    def extract_message(
        self,
        message: Any,
        content: str | None,
    ) -> tuple[str | None, str | None, dict[str, Any]]:
        """提取 DeepSeek reasoning，并保留工具续轮需要的原始字段。"""

        reasoning = _get_field(message, "reasoning_content")
        if reasoning is None:
            return content, None, {}
        text = str(reasoning)
        return content, text, {"reasoning_content": text}

    def provider_fields_for_tool_call(
        self,
        fields: dict[str, Any],
        request: dict[str, Any],
    ) -> dict[str, Any]:
        """确保 thinking 工具调用的下一轮 assistant 消息字段完整。"""

        # 明确关闭 thinking 时无需 reasoning_content；否则即使服务端没有返回
        # 推理文本，也要补空字符串以满足 DeepSeek 工具续轮消息协议。
        if _deepseek_thinking_disabled(dict(request.get("extra_body") or {})):
            return fields
        if "reasoning_content" in fields:
            return fields
        return {**fields, "reasoning_content": ""}

    def prepare_stream_request(
        self, request: dict[str, Any]
    ) -> dict[str, Any]:
        """创建流式请求，并要求末尾返回 usage 供 Prompt Cache 观测。"""

        stream_request = {**request, "stream": True}
        stream_options = dict(stream_request.get("stream_options") or {})
        stream_options["include_usage"] = True
        stream_request["stream_options"] = stream_options
        return stream_request


class DashScopeStrategy(ProviderStrategy):
    """适配 DashScope/Qwen 兼容接口的 thinking 关闭语义。"""

    def prepare_request(
        self,
        request: dict[str, Any],
        extra_body: dict[str, Any],
        *,
        disable_thinking: bool,
    ) -> None:
        """单次禁用时清理其他推理字段，并发送 DashScope 专属开关。"""

        if disable_thinking:
            _drop_thinking_keys(extra_body)
            extra_body["enable_thinking"] = False
        if extra_body:
            request["extra_body"] = extra_body


class LLMProvider:
    """封装 OpenAI Chat Completions 兼容接口。

    Provider 是 SDK 与业务之间的适配层：上游只使用 BeanAgent 自己的
    ``LLMResponse`` 和 ``ToolCall``，不直接依赖 OpenAI SDK 的对象类型。
    """

    def __init__(
        self,
        config: LLMConfig,
        *,
        stream_idle_timeout_s: float | None = None,
        max_retries: int = 1,
        force_disable_thinking: bool = False,
        payload_snapshot_enabled: bool | None = None,
    ) -> None:
        """使用注入的配置创建客户端，不在模块内读取 TOML 或环境变量。"""

        normalized_base_url = _normalize_openai_base_url(config.base_url)
        self._client = AsyncOpenAI(
            api_key=config.api_key,
            base_url=normalized_base_url,
        )
        self._base_url = normalized_base_url or ""
        self._provider_name = config.provider
        self._model = config.model
        self._max_tokens = config.max_tokens
        self._context_window = max(0, int(config.context_window))
        self._system_prompt = config.system_prompt
        self._extra_body = dict(config.extra_body)

        # request_timeout 限制“创建请求/获取响应”的等待时间；stream idle
        # timeout 限制流建立后相邻两个 chunk 之间的等待时间，两者含义不同。
        self._request_timeout_s = max(1.0, float(config.request_timeout_s))
        self._stream_idle_timeout_s = max(
            0.001,
            float(
                config.request_timeout_s
                if stream_idle_timeout_s is None
                else stream_idle_timeout_s
            ),
        )
        # max_retries 表示首次请求之外允许追加的次数，默认 1 即最多请求两次。
        self._max_retries = max(0, int(max_retries))
        self._force_disable_thinking = bool(force_disable_thinking)
        self._payload_snapshot_enabled = (
            _LLM_PAYLOAD_SNAPSHOT_ENABLED
            if payload_snapshot_enabled is None
            else bool(payload_snapshot_enabled)
        )
        self._closed = False

    @property
    def model(self) -> str:
        return self._model

    @property
    def max_tokens(self) -> int:
        return self._max_tokens

    @property
    def context_window(self) -> int:
        """返回模型上下文窗口；0 表示配置未提供可靠容量。"""

        return self._context_window

    def estimate_context_tokens(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> int:
        """估算完整输入 payload，供 compaction gate 在请求前使用。"""

        return estimate_payload_tokens(messages, tools)

    def estimate_appended_message_tokens(
        self,
        messages: list[dict[str, Any]],
    ) -> int:
        """估算追加消息的 token，供未来 usage delta 校准复用。"""

        return estimate_tokens(messages) + len(messages) * 4

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        model: str | None = None,
        max_tokens: int | None = None,
        tool_choice: str | dict[str, Any] = "auto",
        extra_body: dict[str, Any] | None = None,
        disable_thinking: bool = False,
        on_content_delta: StreamCallback | None = None,
    ) -> LLMResponse:
        """调用模型；传入增量回调时使用流式接口，否则返回普通响应。

        ``model`` 和 ``max_tokens`` 允许记忆整理等内部任务复用同一 Provider，
        未覆盖时使用 ``LLMConfig`` 中的主模型参数。
        """

        # 只复制外层列表；策略会逐条复制消息，避免污染 Session 历史。
        selected_model = model or self._model
        strategy = _select_provider_strategy(
            provider_name=self._provider_name,
            base_url=self._base_url,
            model=selected_model,
        )

        full_messages = list(messages)
        already_has_system = bool(
            full_messages and full_messages[0].get("role") == "system"
        )
        if self._system_prompt and not already_has_system:
            full_messages = [
                {"role": "system", "content": self._system_prompt},
                *full_messages,
            ]

        # 多个 system 消息会改变供应商兼容性和 Prompt Cache 前缀，统一合并。
        full_messages = _merge_leading_system_messages(full_messages)
        full_messages = strategy.normalize_messages(full_messages)

        request: dict[str, Any] = {
            "model": selected_model,
            "max_tokens": max_tokens if max_tokens is not None else self._max_tokens,
            "messages": full_messages,
        }
        if tools:
            request["tools"] = tools
            request["tool_choice"] = tool_choice

        # 合并顺序为全局配置 < 单次调用。使用浅拷贝足以保护顶层键；策略只
        # 替换 thinking 字典，不修改调用方传入的嵌套对象。
        merged_extra_body = dict(self._extra_body)
        if extra_body:
            merged_extra_body.update(extra_body)
        strategy.prepare_request(
            request,
            merged_extra_body,
            disable_thinking=self._force_disable_thinking or disable_thinking,
        )

        if on_content_delta is not None:
            return await self._chat_streaming(request, on_content_delta, strategy)
        return await self._chat_non_streaming(request, strategy)

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        model: str | None = None,
        max_tokens: int | None = None,
        tool_choice: str | dict[str, Any] = "auto",
        extra_body: dict[str, Any] | None = None,
        disable_thinking: bool = False,
    ) -> LLMResponse:
        """执行一次明确的非流式调用。

        这是 QueryRewriter、记忆提取等后台任务的便捷入口，内部仍复用
        ``chat()`` 的请求组装逻辑，避免两条路径产生不同的默认参数。
        """

        return await self.chat(
            messages,
            tools,
            model=model,
            max_tokens=max_tokens,
            tool_choice=tool_choice,
            extra_body=extra_body,
            disable_thinking=disable_thinking,
        )

    async def close(self) -> None:
        """幂等关闭底层 HTTP 客户端。

        只有关闭成功后才记录 ``_closed``；如果底层关闭失败，调用方仍可在
        shutdown 清理流程中再次尝试，而不是误以为资源已经释放。
        """

        if self._closed:
            return
        await self._client.close()
        self._closed = True

    async def _chat_non_streaming(
        self,
        request: dict[str, Any],
        strategy: ProviderStrategy,
    ) -> LLMResponse:
        """请求并解析单个 Chat Completion 响应。"""

        response = await self._create_with_retry(request)

        choices = _get_field(response, "choices") or []
        if not choices:
            raise ValueError("LLM 响应不包含 choices")

        message = _get_field(choices[0], "message")
        if message is None:
            raise ValueError("LLM 响应不包含 message")

        tool_calls = _parse_tool_calls(_get_field(message, "tool_calls") or [])
        content, thinking, provider_fields = strategy.extract_message(
            message,
            _optional_text(_get_field(message, "content")),
        )
        if tool_calls:
            provider_fields = strategy.provider_fields_for_tool_call(
                provider_fields, request
            )
        prompt_tokens, hit_tokens = _extract_cache_usage(
            _get_field(response, "usage")
        )
        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            thinking=thinking,
            cache_prompt_tokens=prompt_tokens,
            cache_hit_tokens=hit_tokens,
            provider_fields=provider_fields,
        )

    async def _chat_streaming(
        self,
        request: dict[str, Any],
        on_content_delta: StreamCallback,
        strategy: ProviderStrategy,
    ) -> LLMResponse:
        """消费流式 chunk，并聚合为与非流式路径一致的结果。"""

        # include_usage 让 OpenAI 兼容服务在末尾发送无 choices 的 usage chunk，
        # Provider 会保留它用于观测稳定 Prompt 前缀的缓存命中情况。
        stream = await self._create_with_retry(
            strategy.prepare_stream_request(request)
        )
        stream_iterator = aiter(stream)

        content_parts: list[str] = []
        thinking_parts: list[str] = []

        # 一个 chunk 可包含多个工具，每个工具也可能跨多个 chunk。
        tool_call_chunks: dict[int, dict[str, str]] = {}
        tool_call_seen = False
        cache_prompt_tokens: int | None = None
        cache_hit_tokens: int | None = None

        while True:
            try:
                # 该超时限制相邻 chunk 的等待时间，而不是整个流的总时长。
                chunk = await asyncio.wait_for(
                    anext(stream_iterator), timeout=self._stream_idle_timeout_s
                )
            except StopAsyncIteration:
                break

            # 开启 include_usage 后，最后一个 chunk 往往只有 usage、choices=[]。
            # 因此必须先提取 usage，再判断 choices，否则会丢失缓存指标。
            prompt_tokens, hit_tokens = _extract_cache_usage(
                _get_field(chunk, "usage")
            )
            if prompt_tokens is not None:
                cache_prompt_tokens = prompt_tokens
                cache_hit_tokens = hit_tokens

            choices = _get_field(chunk, "choices") or []
            if not choices:
                continue
            delta = _get_field(choices[0], "delta")
            if delta is None:
                continue

            thinking_piece = _optional_text(_get_field(delta, "reasoning_content"))
            if thinking_piece:
                thinking_parts.append(thinking_piece)
                if not tool_call_seen:
                    await on_content_delta({"thinking_delta": thinking_piece})

            # OpenAI 会把一个工具调用的 id、name 和 JSON arguments 拆到多个
            # chunk。必须按 index 聚合，不能按到达顺序直接创建 ToolCall。
            for tool_delta in _iter_tool_call_deltas(delta):
                tool_call_seen = True
                index = int(tool_delta["index"])
                slot = tool_call_chunks.setdefault(index, {})
                for key in ("id", "name", "arguments"):
                    piece = str(tool_delta[key])
                    if piece:
                        slot[key] = slot.get(key, "") + piece

            # 工具调用开始后，这一轮尚不是最终回答。仍累积供应商返回的文本
            # 以保证响应完整，但不再推给前端，避免显示随后会被工具结果替代的
            # 临时内容。顺序固定为 reasoning -> tools -> content。
            content_piece = _optional_text(_get_field(delta, "content"))
            if content_piece:
                content_parts.append(content_piece)
                if not tool_call_seen:
                    await on_content_delta({"content_delta": content_piece})

        # 流结束后才解析 arguments JSON，因为中间片段通常不是合法 JSON。
        # 按 index 排序可保持模型声明多个工具调用时的原始顺序。
        tool_calls: list[ToolCall] = []
        for index in sorted(tool_call_chunks):
            item = tool_call_chunks[index]
            tool_calls.append(
                ToolCall(
                    id=item.get("id", ""),
                    name=item.get("name", ""),
                    arguments=_parse_arguments(item.get("arguments", "") or "{}"),
                )
            )

        content = "".join(content_parts).strip() or None
        thinking = "".join(thinking_parts).strip() or None
        content, parsed_thinking, provider_fields = strategy.extract_message(
            {"reasoning_content": thinking} if thinking is not None else {},
            content,
        )
        thinking = parsed_thinking if parsed_thinking is not None else thinking
        if tool_calls:
            provider_fields = strategy.provider_fields_for_tool_call(
                provider_fields, request
            )

        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            thinking=thinking,
            cache_prompt_tokens=cache_prompt_tokens,
            cache_hit_tokens=cache_hit_tokens,
            provider_fields=provider_fields,
        )

    async def _create_with_retry(self, request: dict[str, Any]) -> Any:
        """执行请求，只重试临时网络、限流和服务端错误。

        重试范围只覆盖“创建响应或流对象”。流建立后的 chunk 空闲超时直接
        抛给上层，因为此时从头重试可能让用户收到重复的文本片段。
        """

        _save_llm_payload_snapshot(
            request,
            enabled=self._payload_snapshot_enabled,
        )
        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                return await asyncio.wait_for(
                    self._client.chat.completions.create(**request),
                    timeout=self._request_timeout_s,
                )
            except Exception as error:
                last_error = error
                logger.warning(
                    "LLM 请求失败 model=%s stream=%s base_url=%s tools=%d error=%s",
                    request.get("model"),
                    bool(request.get("stream")),
                    self._base_url,
                    len(request.get("tools") or []),
                    type(error).__name__,
                )
                if self._is_safety_error(error):
                    raise ContentSafetyError(str(error)) from error
                # 上下文超限需要交给 Pipeline 做裁剪降级；若在这里重试同一
                # payload 只会重复失败，因此必须先于通用重试判断转换异常。
                if self._is_context_length_error(error):
                    raise ContextLengthError(str(error)) from error

                exhausted = attempt >= self._max_retries
                if exhausted or not self._is_retryable(error):
                    raise

                wait_seconds = min(8.0, 1.0 * (2**attempt))
                logger.warning(
                    "LLM 请求暂时失败，将重试 attempt=%d/%d wait=%.1fs error=%s",
                    attempt + 1,
                    self._max_retries + 1,
                    wait_seconds,
                    type(error).__name__,
                )
                await asyncio.sleep(wait_seconds)

        raise RuntimeError("LLM 请求在未返回结果的情况下结束") from last_error

    @staticmethod
    def _is_context_length_error(error: Exception) -> bool:
        """根据兼容服务常见错误文本识别上下文超限。"""

        text = str(error).lower()
        return any(keyword in text for keyword in _CONTEXT_LENGTH_KEYWORDS)

    @staticmethod
    def _is_safety_error(error: Exception) -> bool:
        """识别常见供应商的内容安全拒绝错误码。"""

        text = str(error)
        return any(code in text for code in _SAFETY_ERROR_CODES)

    @staticmethod
    def _is_retryable(error: Exception) -> bool:
        """判断错误是否可能在短暂等待后恢复。"""

        if isinstance(error, (TimeoutError, asyncio.TimeoutError)):
            return True
        if getattr(error, "status_code", None) in _RETRYABLE_STATUS_CODES:
            return True
        text = str(error).lower()
        return any(keyword in text for keyword in _RETRYABLE_KEYWORDS)


def _merge_leading_system_messages(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """合并开头连续的 system 消息，其他消息及顺序保持不变。"""

    system_contents: list[str] = []
    index = 0
    while index < len(messages) and messages[index].get("role") == "system":
        content = messages[index].get("content")
        if isinstance(content, str) and content:
            system_contents.append(content)
        index += 1

    merged: list[dict[str, Any]] = []
    if system_contents:
        merged.append({"role": "system", "content": "\n\n".join(system_contents)})
    merged.extend(messages[index:])
    return merged if merged else list(messages)


def _normalize_openai_base_url(base_url: str | None) -> str | None:
    """把误填的具体 completion 端点裁剪为 SDK 所需的 API 根地址。"""

    text = (base_url or "").strip()
    if not text:
        return None
    parsed = urlsplit(text)
    path = parsed.path.rstrip("/")
    for suffix in ("/chat/completions", "/completions", "/responses"):
        if path.endswith(suffix):
            path = path[: -len(suffix)].rstrip("/")
            break
    return urlunsplit(
        (parsed.scheme, parsed.netloc, path, parsed.query, parsed.fragment)
    )


def _select_provider_strategy(
    *,
    provider_name: str,
    base_url: str,
    model: str,
) -> ProviderStrategy:
    """根据配置特征选择策略，兼容 provider 名称缺失的旧配置。"""

    provider_text = f"{provider_name} {base_url} {model}".lower()
    if "deepseek" in provider_text:
        return DeepSeekStrategy()
    if (
        "dashscope.aliyuncs.com" in provider_text
        or "dashscope" in provider_text
        or "xiaomimimo.com" in provider_text
    ):
        return DashScopeStrategy()
    return ProviderStrategy()


def _drop_thinking_keys(extra_body: dict[str, Any]) -> None:
    """删除通用接口不应收到的 thinking 扩展参数。"""

    for key in ("enable_thinking", "thinking", "reasoning_effort"):
        extra_body.pop(key, None)


def _strip_reasoning_content(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """非 DeepSeek 请求不发送其专属 reasoning_content 字段。"""

    return [
        {key: value for key, value in message.items() if key != "reasoning_content"}
        for message in messages
    ]


def _deepseek_thinking_disabled(extra_body: dict[str, Any]) -> bool:
    """判断 DeepSeek 原生 thinking 配置是否明确关闭。"""

    thinking = extra_body.get("thinking")
    if not isinstance(thinking, dict):
        return False
    return str(thinking.get("type", "") or "").lower() == "disabled"


def _deepseek_thinking_enabled(extra_body: dict[str, Any]) -> bool:
    """判断 DeepSeek 原生 thinking 配置是否明确开启。"""

    thinking = extra_body.get("thinking")
    if not isinstance(thinking, dict):
        return False
    return str(thinking.get("type", "") or "").lower() == "enabled"


def _normalize_deepseek_effort(value: str) -> str:
    """把通用推理强度名称转换为 DeepSeek 接受的名称。"""

    effort = value.strip().lower()
    return "max" if effort == "xhigh" else effort


def _ensure_deepseek_reasoning_content(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """为 thinking 历史中的 assistant 消息补齐 reasoning_content。

    Session 中可能存在启用 thinking 之前保存的旧消息。DeepSeek 要求推理
    多轮中的 assistant 消息具有该字段，因此缺失时补空字符串；已有内容
    原样保留。函数总是复制消息，调用方持有的历史不会被修改。
    """

    normalized: list[dict[str, Any]] = []
    for message in messages:
        item = dict(message)
        if item.get("role") == "assistant" and "reasoning_content" not in item:
            item["reasoning_content"] = ""
        normalized.append(item)
    return normalized


def _normalize_chat_messages(
    messages: list[dict[str, Any]],
    *,
    fill_tool_call_content: bool = True,
) -> list[dict[str, Any]]:
    """复制消息并把用户、助手、工具角色的 None content 规范为字符串。

    参数保留通用策略行为。DeepSeekStrategy 传 False，不为工具
    调用伪造说明文本；未来如果加入其他 Provider，可复用默认 True 行为。
    """

    normalized: list[dict[str, Any]] = []
    for message in messages:
        item = dict(message)
        role = str(item.get("role", "") or "")
        content = item.get("content")

        if fill_tool_call_content and role == "assistant" and item.get("tool_calls"):
            if content is None or (isinstance(content, str) and not content.strip()):
                tool_calls = item.get("tool_calls") or []
                first = tool_calls[0] if isinstance(tool_calls, list) and tool_calls else {}
                function = first.get("function") if isinstance(first, dict) else {}
                tool_name = ""
                if isinstance(function, dict):
                    tool_name = str(function.get("name", "") or "")
                item["content"] = f"调用工具 {tool_name}" if tool_name else "调用工具"
        elif role in {"user", "assistant", "tool"} and content is None:
            item["content"] = ""

        normalized.append(item)
    return normalized


def _strip_image_url_blocks(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """移除 DeepSeek 文本接口不支持的 image_url，同时保留文本块。

    不静默丢弃图片：追加中文占位说明，让模型知道用户原消息包含图片，
    也方便日志和历史回放定位能力边界。
    """

    normalized: list[dict[str, Any]] = []
    for message in messages:
        item = dict(message)
        content = item.get("content")
        if isinstance(content, list):
            text_parts: list[str] = []
            image_count = 0
            for block in content:
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type")
                if block_type == "text":
                    text = block.get("text")
                    if isinstance(text, str) and text:
                        text_parts.append(text)
                elif block_type == "image_url":
                    image_count += 1
            if image_count:
                text_parts.append(
                    f"[已移除 {image_count} 个 image_url 图片块："
                    "DeepSeek 当前接口只接受文本消息。]"
                )
            item["content"] = "\n".join(text_parts)
        normalized.append(item)
    return normalized


def _get_field(value: Any, name: str) -> Any:
    """同时读取 SDK 对象属性和兼容服务返回的字典字段。

    正式 OpenAI SDK 通常返回 Pydantic 对象，而单元测试和部分兼容网关可能
    返回普通字典。集中兼容可避免解析流程到处出现 isinstance 分支。
    """

    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


def _optional_text(value: Any) -> str | None:
    """只接受非空字符串，过滤 SDK 可能返回的空值或其他类型。"""

    return value if isinstance(value, str) and value else None


def _parse_arguments(raw_arguments: Any) -> dict[str, Any]:
    """把工具参数 JSON 转为字典，并拒绝不符合工具协议的顶层类型。"""

    # 空参数按空对象处理；非法 JSON 不静默吞掉，否则工具可能在错误参数下
    # 执行。解析异常会终止当前模型响应并由上层记录。
    parsed = json.loads(str(raw_arguments or "{}"))
    if not isinstance(parsed, dict):
        raise ValueError("工具调用 arguments 必须是 JSON 对象")
    return parsed


def _parse_tool_calls(raw_tool_calls: list[Any]) -> list[ToolCall]:
    """解析非流式响应中的完整工具调用列表。"""

    # 保留 API 返回顺序，Pipeline 将按该顺序发布 ToolCallStarted 并执行工具。
    result: list[ToolCall] = []
    for item in raw_tool_calls:
        function = _get_field(item, "function")
        result.append(
            ToolCall(
                id=str(_get_field(item, "id") or ""),
                name=str(_get_field(function, "name") or ""),
                arguments=_parse_arguments(_get_field(function, "arguments")),
            )
        )
    return result


def _iter_tool_call_deltas(delta: Any) -> list[dict[str, str | int]]:
    """把对象或字典形式的流式工具片段规范成统一结构。"""

    result: list[dict[str, str | int]] = []
    for fallback_index, item in enumerate(_get_field(delta, "tool_calls") or []):
        function = _get_field(item, "function")
        # 不能写成 ``raw_index or fallback_index``：合法 index=0 是假值，
        # 在同一 chunk 的非首项中会被错误替换，导致两个工具片段串到一起。
        raw_index = _get_field(item, "index")
        result.append(
            {
                "index": int(fallback_index if raw_index is None else raw_index),
                "id": str(_get_field(item, "id") or ""),
                "name": str(_get_field(function, "name") or ""),
                "arguments": str(_get_field(function, "arguments") or ""),
            }
        )
    return result


def _coerce_int(value: Any) -> int | None:
    """把 usage 中可能为字符串的 token 数安全转换为整数。"""

    # bool 是 int 的子类，但 True/False 不是合法 token 数，需要显式排除。
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _save_llm_payload_snapshot(
    request: dict[str, Any],
    *,
    enabled: bool | None = None,
) -> Path | None:
    """按显式开关把请求写入临时目录，供本地协议调试。

    快照包含完整 messages、tools 和扩展参数，可能涉及隐私。默认关闭，
    启用方负责临时目录权限、使用范围和清理策略。
    """

    snapshot_enabled = (
        _LLM_PAYLOAD_SNAPSHOT_ENABLED if enabled is None else bool(enabled)
    )
    if not snapshot_enabled:
        return None

    try:
        payload = json.dumps(request, ensure_ascii=False, indent=2, default=str)
        _PAYLOAD_SNAPSHOT_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        sequence = next(_PAYLOAD_SNAPSHOT_SEQ)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        path = _PAYLOAD_SNAPSHOT_DIR / (
            f"{timestamp}-{os.getpid()}-{sequence:06d}.json"
        )
        path.write_text(payload, encoding="utf-8")
        _LAST_PAYLOAD_PATH.write_text(payload, encoding="utf-8")
        logger.info("LLM 请求快照已保存 path=%s", path)
        return path
    except Exception as error:
        # 调试能力不能影响主请求；失败时仅留日志供开发者定位。
        logger.warning("LLM 请求快照保存失败 error=%s", error)
        return None


def _extract_cache_usage(usage: Any) -> tuple[int | None, int | None]:
    """提取总 Prompt token 与缓存命中 token。

    DeepSeek/DashScope 兼容接口可能直接给出 hit/miss；OpenAI 则把命中量
    放在 ``prompt_tokens_details.cached_tokens``。两种格式统一后，Pipeline
    才能用同一组指标判断 Prompt 稳定前缀是否真正命中缓存。
    """

    if usage is None:
        return None, None

    # 第一种格式直接提供缓存命中与未命中量，总 Prompt token 由两者相加。
    hit_tokens = _coerce_int(_get_field(usage, "prompt_cache_hit_tokens"))
    miss_tokens = _coerce_int(_get_field(usage, "prompt_cache_miss_tokens"))
    if hit_tokens is not None or miss_tokens is not None:
        hit = hit_tokens or 0
        miss = miss_tokens or 0
        return hit + miss, hit

    # 第二种是 OpenAI 格式：总量在 prompt_tokens，命中量位于 details。
    prompt_tokens = _coerce_int(_get_field(usage, "prompt_tokens"))
    details = _get_field(usage, "prompt_tokens_details")
    cached_tokens = _coerce_int(_get_field(details, "cached_tokens"))
    if prompt_tokens is None or cached_tokens is None:
        return None, None
    return prompt_tokens, cached_tokens


def create_vision_provider(config: VisionConfig | None) -> LLMProvider | None:
    """为独立 VL 配置创建专用 Provider；未配置模型时不启用视觉工具。"""

    if config is None or not config.model.strip():
        return None
    # VL 使用独立客户端，避免视觉模型的地址、密钥和超时污染主对话模型。
    # system_prompt 留空且 extra_body 独立，视觉工具每次通过 prompt 明确任务。
    return LLMProvider(
        LLMConfig(
            provider=config.provider,
            model=config.model,
            api_key=config.api_key,
            base_url=config.base_url,
            max_tokens=config.max_tokens,
            request_timeout_s=config.request_timeout_s,
        )
    )


__all__ = [
    "ContentSafetyError",
    "ContextLengthError",
    "DashScopeStrategy",
    "DeepSeekStrategy",
    "LLMProvider",
    "LLMResponse",
    "ProviderStrategy",
    "ToolCall",
    "create_vision_provider",
]
