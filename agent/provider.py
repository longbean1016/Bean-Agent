"""OpenAI 兼容的语言模型调用层。

统一处理普通回复、流式增量、工具调用、请求重试和 Prompt Cache 指标，
使 Agent 核心循环不依赖具体模型供应商的 SDK 返回结构。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from openai import AsyncOpenAI

from agent.config_models import LLMConfig

logger = logging.getLogger(__name__)

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


class ContextLengthError(Exception):
    """模型因输入超过上下文窗口而拒绝请求。"""


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
    ) -> None:
        """使用注入的配置创建客户端，不在模块内读取 TOML 或环境变量。"""

        # 客户端在应用启动时创建一次，退出时由 FastAPI lifespan 调用 close()。
        # Provider 不负责读取环境变量；config.py 已经完成密钥占位符解析。
        self._client = AsyncOpenAI(api_key=config.api_key, base_url=config.base_url)

        # 模型和 token 上限是主对话的默认值。chat()/complete() 仍允许单次
        # 覆盖，以便未来 QueryRewriter 等轻量任务复用同一个 HTTP 客户端。
        self._model = config.model
        self._max_tokens = config.max_tokens
        self._system_prompt = config.system_prompt

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
        self._closed = False

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        model: str | None = None,
        max_tokens: int | None = None,
        tool_choice: str | dict[str, Any] = "auto",
        on_content_delta: StreamCallback | None = None,
    ) -> LLMResponse:
        """调用模型；传入增量回调时使用流式接口，否则返回普通响应。

        ``model`` 和 ``max_tokens`` 允许记忆整理等内部任务复用同一 Provider，
        未覆盖时使用 ``LLMConfig`` 中的主模型参数。
        """

        # 复制外层列表，保证追加 system prompt 不会污染 Pipeline 保存的原消息。
        # 消息内容本身不在本函数修改，因此无需成本更高的 deepcopy。
        full_messages = list(messages)
        already_has_system = bool(
            full_messages and full_messages[0].get("role") == "system"
        )
        if self._system_prompt and not already_has_system:
            # PromptAssembler 正常会提供 system 消息；这里只为独立调用提供兜底，
            # 并创建新列表，避免修改调用方用于 Prompt Cache 的稳定消息序列。
            full_messages = [
                {"role": "system", "content": self._system_prompt},
                *full_messages,
            ]

        # 在这一层统一生成 OpenAI 请求体。调用方没有传 tools 时不发送
        # tool_choice，避免部分兼容服务拒绝“没有 tools 却指定 auto”的请求。
        request: dict[str, Any] = {
            "model": model or self._model,
            "max_tokens": max_tokens if max_tokens is not None else self._max_tokens,
            "messages": full_messages,
        }
        if tools:
            request["tools"] = tools
            request["tool_choice"] = tool_choice

        # 是否提供回调是流式模式的唯一开关：Pipeline 需要前端逐字显示时
        # 传入回调；记忆整理等只关心完整结果的任务直接走非流式路径。
        if on_content_delta is not None:
            return await self._chat_streaming(request, on_content_delta)
        return await self._chat_non_streaming(request)

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        model: str | None = None,
        max_tokens: int | None = None,
        tool_choice: str | dict[str, Any] = "auto",
    ) -> LLMResponse:
        """执行一次明确的非流式调用。

        这是 QueryRewriter、Consolidator 等后台任务的便捷入口，内部仍复用
        ``chat()`` 的请求组装逻辑，避免两条路径产生不同的默认参数。
        """

        return await self.chat(
            messages,
            tools,
            model=model,
            max_tokens=max_tokens,
            tool_choice=tool_choice,
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
        self, request: dict[str, Any]
    ) -> LLMResponse:
        """请求并解析单个 Chat Completion 响应。"""

        response = await self._create_with_retry(request)

        # Chat Completions 正常至少包含一个 choice。与其在下方触发难理解的
        # IndexError，这里给出明确协议错误，方便定位供应商兼容性问题。
        choices = _get_field(response, "choices") or []
        if not choices:
            raise ValueError("LLM 响应不包含 choices")

        message = _get_field(choices[0], "message")
        if message is None:
            raise ValueError("LLM 响应不包含 message")

        # 非流式响应中的 tool_calls 已是完整对象，只需解析 arguments JSON；
        # 流式响应则会在 _chat_streaming() 中处理分片拼接。
        tool_calls = _parse_tool_calls(_get_field(message, "tool_calls") or [])
        prompt_tokens, hit_tokens = _extract_cache_usage(
            _get_field(response, "usage")
        )
        return LLMResponse(
            content=_optional_text(_get_field(message, "content")),
            tool_calls=tool_calls,
            thinking=_optional_text(_get_field(message, "reasoning_content")),
            cache_prompt_tokens=prompt_tokens,
            cache_hit_tokens=hit_tokens,
        )

    async def _chat_streaming(
        self,
        request: dict[str, Any],
        on_content_delta: StreamCallback,
    ) -> LLMResponse:
        """消费流式 chunk，并聚合为与非流式路径一致的结果。"""

        # include_usage 让 OpenAI 兼容服务在末尾发送无 choices 的 usage chunk，
        # Provider 会保留它用于观测稳定 Prompt 前缀的缓存命中情况。
        stream = await self._create_with_retry(
            {
                **request,
                "stream": True,
                "stream_options": {"include_usage": True},
            }
        )
        stream_iterator = aiter(stream)

        # 文本和思考内容按到达顺序累积，结束后再组合完整响应；与此同时，
        # 每个有效片段立即通过回调交给 EventBus/WebSocket，供前端逐步渲染。
        content_parts: list[str] = []
        thinking_parts: list[str] = []

        # key 是 SDK 提供的 tool call index；value 保存尚未完整的字符串片段。
        # 一个 chunk 可能同时包含多个工具，每个工具也可能跨多个 chunk。
        tool_call_chunks: dict[int, dict[str, str]] = {}
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

            # reasoning_content 是 DeepSeek 等推理模型常用的扩展字段；普通
            # OpenAI 模型没有该字段时 _get_field 会安全返回 None。
            thinking_piece = _optional_text(_get_field(delta, "reasoning_content"))
            if thinking_piece:
                thinking_parts.append(thinking_piece)
                await on_content_delta({"thinking_delta": thinking_piece})

            content_piece = _optional_text(_get_field(delta, "content"))
            if content_piece:
                content_parts.append(content_piece)
                await on_content_delta({"content_delta": content_piece})

            # OpenAI 会把一个工具调用的 id、name 和 JSON arguments 拆到多个
            # chunk。必须按 index 聚合，不能按到达顺序直接创建 ToolCall。
            for tool_delta in _iter_tool_call_deltas(delta):
                index = int(tool_delta["index"])
                slot = tool_call_chunks.setdefault(index, {})
                for key in ("id", "name", "arguments"):
                    piece = str(tool_delta[key])
                    if piece:
                        slot[key] = slot.get(key, "") + piece

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

        # 即使采用流式传输，Provider 最终仍返回完整 LLMResponse，供 Pipeline
        # 决定结束本轮还是执行工具后进入下一次 ReAct 迭代。
        return LLMResponse(
            content="".join(content_parts).strip() or None,
            tool_calls=tool_calls,
            thinking="".join(thinking_parts).strip() or None,
            cache_prompt_tokens=cache_prompt_tokens,
            cache_hit_tokens=cache_hit_tokens,
        )

    async def _create_with_retry(self, request: dict[str, Any]) -> Any:
        """执行请求，只重试临时网络、限流和服务端错误。

        重试范围只覆盖“创建响应或流对象”。流建立后的 chunk 空闲超时直接
        抛给上层，因为此时从头重试可能让用户收到重复的文本片段。
        """

        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                return await asyncio.wait_for(
                    self._client.chat.completions.create(**request),
                    timeout=self._request_timeout_s,
                )
            except Exception as error:
                last_error = error
                # 上下文超限需要交给 Pipeline 做裁剪降级；若在这里重试同一
                # payload 只会重复失败，因此必须先于通用重试判断转换异常。
                if self._is_context_length_error(error):
                    raise ContextLengthError(str(error)) from error

                # 鉴权、请求参数、内容安全等错误不属于可恢复错误，立即原样
                # 抛出，既减少无效等待，也保留 SDK 异常中的诊断信息。
                exhausted = attempt >= self._max_retries
                if exhausted or not self._is_retryable(error):
                    raise

                # 采用 1、2、4、8 秒指数退避并封顶，避免限流时持续打满服务。
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
    def _is_retryable(error: Exception) -> bool:
        """判断错误是否可能在短暂等待后恢复。"""

        if isinstance(error, (TimeoutError, asyncio.TimeoutError)):
            return True
        if getattr(error, "status_code", None) in _RETRYABLE_STATUS_CODES:
            return True
        text = str(error).lower()
        return any(keyword in text for keyword in _RETRYABLE_KEYWORDS)


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


__all__ = ["ContextLengthError", "LLMProvider", "LLMResponse", "ToolCall"]
