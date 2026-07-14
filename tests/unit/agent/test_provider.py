"""LLM Provider 的离线协议测试。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from agent.config_models import LLMConfig
from agent.provider import ContextLengthError, LLMProvider


def _ns(**values: Any) -> SimpleNamespace:
    """用轻量对象模拟 OpenAI SDK 返回的数据模型。"""

    return SimpleNamespace(**values)


class _Completions:
    """记录请求并按脚本返回结果或抛出异常。"""

    def __init__(self, *results: object) -> None:
        self.results = list(results)
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> object:
        self.calls.append(kwargs)
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class _Client:
    """提供 Provider 实际使用的最小 OpenAI 客户端结构。"""

    def __init__(self, *results: object) -> None:
        self.completions = _Completions(*results)
        self.chat = _ns(completions=self.completions)
        self.close = AsyncMock()


class _Stream:
    """把预设 chunk 暴露为异步迭代器。"""

    def __init__(self, *chunks: object) -> None:
        self._chunks = iter(chunks)

    def __aiter__(self) -> AsyncIterator[object]:
        return self

    async def __anext__(self) -> object:
        try:
            return next(self._chunks)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


def _provider(client: _Client, **config_overrides: Any) -> LLMProvider:
    config = LLMConfig(api_key="test-key", **config_overrides)
    provider = LLMProvider(config)
    provider._client = client
    return provider


def test_constructor_injects_llm_config_into_openai_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    client = _Client()

    def create_client(**kwargs: Any) -> _Client:
        captured.update(kwargs)
        return client

    monkeypatch.setattr("agent.provider.AsyncOpenAI", create_client)

    LLMProvider(
        LLMConfig(
            api_key="secret",
            base_url="https://proxy.example/v1",
            model="test-model",
        )
    )

    assert captured == {
        "api_key": "secret",
        "base_url": "https://proxy.example/v1",
    }


@pytest.mark.asyncio
async def test_complete_builds_request_and_parses_tool_call_and_cache() -> None:
    message = _ns(
        content=None,
        reasoning_content="需要查询天气",
        tool_calls=[
            _ns(
                id="call-1",
                function=_ns(name="weather", arguments='{"city":"上海"}'),
            )
        ],
    )
    response = _ns(
        choices=[_ns(message=message)],
        usage=_ns(
            prompt_tokens=120,
            prompt_tokens_details=_ns(cached_tokens=80),
        ),
    )
    client = _Client(response)
    provider = _provider(
        client,
        model="deepseek-test",
        max_tokens=2048,
        system_prompt="你是测试助手",
    )
    tools = [{"type": "function", "function": {"name": "weather"}}]

    result = await provider.complete(
        messages=[{"role": "user", "content": "天气如何"}], tools=tools
    )

    request = client.completions.calls[0]
    assert request["model"] == "deepseek-test"
    assert request["max_tokens"] == 2048
    assert request["messages"][0] == {
        "role": "system",
        "content": "你是测试助手",
    }
    assert request["tools"] == tools
    assert request["tool_choice"] == "auto"
    assert "stream" not in request
    assert result.content is None
    assert result.thinking == "需要查询天气"
    assert result.tool_calls[0].id == "call-1"
    assert result.tool_calls[0].name == "weather"
    assert result.tool_calls[0].arguments == {"city": "上海"}
    assert result.cache_prompt_tokens == 120
    assert result.cache_hit_tokens == 80


@pytest.mark.asyncio
async def test_chat_streams_deltas_and_assembles_fragmented_tool_calls() -> None:
    stream = _Stream(
        _ns(
            choices=[
                _ns(delta=_ns(reasoning_content="先思考", content=None, tool_calls=[]))
            ],
            usage=None,
        ),
        _ns(
            choices=[_ns(delta=_ns(reasoning_content=None, content="正在", tool_calls=[]))],
            usage=None,
        ),
        _ns(
            choices=[
                _ns(
                    delta=_ns(
                        reasoning_content=None,
                        content=None,
                        tool_calls=[
                            _ns(
                                index=0,
                                id="call-",
                                function=_ns(name="wea", arguments='{"city":'),
                            )
                        ],
                    )
                )
            ],
            usage=None,
        ),
        _ns(
            choices=[
                _ns(
                    delta=_ns(
                        reasoning_content=None,
                        content=None,
                        tool_calls=[
                            _ns(
                                index=0,
                                id="1",
                                function=_ns(name="ther", arguments='"上海"}'),
                            )
                        ],
                    )
                )
            ],
            usage=None,
        ),
        _ns(
            choices=[],
            usage=_ns(prompt_cache_hit_tokens=90, prompt_cache_miss_tokens=30),
        ),
    )
    client = _Client(stream)
    provider = _provider(client)
    deltas: list[dict[str, str]] = []

    async def receive_delta(delta: dict[str, str]) -> None:
        deltas.append(delta)

    result = await provider.chat(
        messages=[{"role": "system", "content": "已有系统提示"}],
        tools=[],
        on_content_delta=receive_delta,
    )

    assert client.completions.calls[0]["stream"] is True
    assert client.completions.calls[0]["stream_options"] == {
        "include_usage": True
    }
    assert deltas == [
        {"thinking_delta": "先思考"},
        {"content_delta": "正在"},
    ]
    assert result.content == "正在"
    assert result.thinking == "先思考"
    assert result.tool_calls[0].id == "call-1"
    assert result.tool_calls[0].name == "weather"
    assert result.tool_calls[0].arguments == {"city": "上海"}
    assert result.cache_prompt_tokens == 120
    assert result.cache_hit_tokens == 90


@pytest.mark.asyncio
async def test_retryable_error_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    retryable_error = RuntimeError("503 server error")
    response = _ns(
        choices=[_ns(message=_ns(content="成功", reasoning_content=None, tool_calls=[]))],
        usage=None,
    )
    client = _Client(retryable_error, response)
    provider = _provider(client)
    sleep = AsyncMock()
    monkeypatch.setattr("agent.provider.asyncio.sleep", sleep)

    result = await provider.complete([{"role": "user", "content": "你好"}])

    assert result.content == "成功"
    assert len(client.completions.calls) == 2
    sleep.assert_awaited_once_with(1.0)


@pytest.mark.asyncio
async def test_context_length_error_is_converted_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _Client(RuntimeError("maximum context length exceeded"))
    provider = _provider(client)
    sleep = AsyncMock()
    monkeypatch.setattr("agent.provider.asyncio.sleep", sleep)

    with pytest.raises(ContextLengthError, match="maximum context length"):
        await provider.complete([{"role": "user", "content": "很长的消息"}])

    assert len(client.completions.calls) == 1
    sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_retryable_error_is_raised_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _Client(RuntimeError("401 invalid api key"))
    provider = _provider(client)
    sleep = AsyncMock()
    monkeypatch.setattr("agent.provider.asyncio.sleep", sleep)

    with pytest.raises(RuntimeError, match="invalid api key"):
        await provider.complete([{"role": "user", "content": "你好"}])

    assert len(client.completions.calls) == 1
    sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_close_is_idempotent() -> None:
    client = _Client()
    provider = _provider(client)

    await provider.close()
    await provider.close()

    client.close.assert_awaited_once()
