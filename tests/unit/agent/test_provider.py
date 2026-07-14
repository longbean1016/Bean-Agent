"""LLM Provider 的离线协议测试。"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from agent.config_models import LLMConfig
import agent.provider as provider_module
from agent.provider import ContentSafetyError, ContextLengthError, LLMProvider


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
async def test_generic_strategy_does_not_send_deepseek_reasoning_fields() -> None:
    response = _ns(
        choices=[_ns(message=_ns(content="完成", tool_calls=[]))],
        usage=None,
    )
    client = _Client(response)
    provider = _provider(client, provider="openai", model="gpt-test")
    messages = [
        {
            "role": "assistant",
            "content": None,
            "reasoning_content": "不能发送",
            "tool_calls": [
                {"function": {"name": "search", "arguments": "{}"}}
            ],
        }
    ]

    await provider.complete(messages)

    sent = client.completions.calls[-1]["messages"][0]
    assert "reasoning_content" not in sent
    assert sent["content"] == "调用工具 search"
    assert messages[0]["reasoning_content"] == "不能发送"


@pytest.mark.asyncio
async def test_generic_strategy_extracts_think_tags() -> None:
    response = _ns(
        choices=[
            _ns(
                message=_ns(
                    content="<think>先分析</think>最终回答",
                    tool_calls=[],
                )
            )
        ],
        usage=None,
    )
    provider = _provider(
        _Client(response), provider="openai", model="gpt-test"
    )

    result = await provider.complete([{"role": "user", "content": "问题"}])

    assert result.thinking == "先分析"
    assert result.content == "最终回答"


@pytest.mark.asyncio
async def test_dashscope_strategy_keeps_enabled_thinking_config() -> None:
    response = _ns(
        choices=[_ns(message=_ns(content="完成", tool_calls=[]))],
        usage=None,
    )
    client = _Client(response)
    provider = _provider(
        client,
        provider="qwen",
        model="qwen-plus",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        extra_body={"enable_thinking": True, "custom": "保留"},
    )

    await provider.complete([{"role": "user", "content": "问题"}])

    assert client.completions.calls[-1]["extra_body"] == {
        "enable_thinking": True,
        "custom": "保留",
    }


@pytest.mark.asyncio
async def test_dashscope_strategy_disables_thinking_for_one_call() -> None:
    response = _ns(
        choices=[_ns(message=_ns(content="完成", tool_calls=[]))],
        usage=None,
    )
    client = _Client(response)
    provider = _provider(
        client,
        provider="qwen",
        model="qwen-plus",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        extra_body={
            "enable_thinking": True,
            "thinking": {"type": "enabled"},
            "reasoning_effort": "high",
            "custom": "保留",
        },
    )

    await provider.complete(
        [{"role": "user", "content": "简单问题"}],
        disable_thinking=True,
    )

    assert client.completions.calls[-1]["extra_body"] == {
        "enable_thinking": False,
        "custom": "保留",
    }


@pytest.mark.asyncio
async def test_mimo_base_url_uses_dashscope_thinking_behavior() -> None:
    response = _ns(
        choices=[_ns(message=_ns(content="完成", tool_calls=[]))],
        usage=None,
    )
    client = _Client(response)
    provider = _provider(
        client,
        provider="",
        model="mimo-v2.5",
        base_url="https://token-plan-cn.xiaomimimo.com/v1",
        extra_body={"enable_thinking": True},
    )

    await provider.complete(
        [{"role": "user", "content": "问题"}],
        disable_thinking=True,
    )

    assert client.completions.calls[-1]["extra_body"] == {
        "enable_thinking": False
    }


@pytest.mark.asyncio
async def test_chat_selects_strategy_from_each_call_model() -> None:
    response = _ns(
        choices=[_ns(message=_ns(content="完成", reasoning_content=None, tool_calls=[]))],
        usage=None,
    )
    client = _Client(response)
    provider = _provider(
        client,
        provider="",
        model="generic-model",
        base_url="https://gateway.example/v1",
        extra_body={"enable_thinking": True},
    )

    await provider.complete(
        [{"role": "assistant", "content": "旧回复"}],
        model="deepseek-v4-flash",
    )

    request = client.completions.calls[-1]
    assert request["extra_body"] == {"thinking": {"type": "enabled"}}
    assert request["messages"][0]["reasoning_content"] == ""


@pytest.mark.asyncio
async def test_leading_system_messages_are_merged() -> None:
    response = _ns(
        choices=[_ns(message=_ns(content="完成", tool_calls=[]))],
        usage=None,
    )
    client = _Client(response)
    provider = _provider(client, provider="openai", model="gpt-test")

    await provider.complete(
        [
            {"role": "system", "content": "规则一"},
            {"role": "system", "content": "规则二"},
            {"role": "user", "content": "问题"},
        ]
    )

    assert client.completions.calls[-1]["messages"] == [
        {"role": "system", "content": "规则一\n\n规则二"},
        {"role": "user", "content": "问题"},
    ]


def test_base_url_removes_completion_endpoint_suffix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def create_client(**kwargs: Any) -> _Client:
        captured.update(kwargs)
        return _Client()

    monkeypatch.setattr("agent.provider.AsyncOpenAI", create_client)

    LLMProvider(
        LLMConfig(
            api_key="test",
            model="test",
            base_url="https://gateway.example/v1/chat/completions",
        )
    )

    assert captured["base_url"] == "https://gateway.example/v1"


@pytest.mark.asyncio
async def test_force_disable_thinking_overrides_every_call() -> None:
    response = _ns(
        choices=[_ns(message=_ns(content="完成", reasoning_content=None, tool_calls=[]))],
        usage=None,
    )
    client = _Client(response)
    config = LLMConfig(
        provider="deepseek",
        model="deepseek-v4-flash",
        api_key="test",
        extra_body={"enable_thinking": True},
    )
    provider = LLMProvider(config, force_disable_thinking=True)
    provider._client = client

    await provider.complete([{"role": "user", "content": "问题"}])

    assert client.completions.calls[-1]["extra_body"] == {
        "thinking": {"type": "disabled"}
    }


@pytest.mark.asyncio
async def test_content_safety_error_is_converted_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _Client(RuntimeError("content_policy_violation"))
    provider = _provider(client)
    sleep = AsyncMock()
    monkeypatch.setattr("agent.provider.asyncio.sleep", sleep)

    with pytest.raises(ContentSafetyError, match="content_policy_violation"):
        await provider.complete([{"role": "user", "content": "问题"}])

    assert len(client.completions.calls) == 1
    sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_payload_snapshot_is_default_off_and_instance_opt_in(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot_dir = tmp_path / "payloads"
    last_snapshot = tmp_path / "last.json"
    monkeypatch.setattr(provider_module, "_PAYLOAD_SNAPSHOT_DIR", snapshot_dir)
    monkeypatch.setattr(provider_module, "_LAST_PAYLOAD_PATH", last_snapshot)

    response_off = _ns(
        choices=[_ns(message=_ns(content="关闭", tool_calls=[]))], usage=None
    )
    provider_off = _provider(_Client(response_off))
    await provider_off.complete([{"role": "user", "content": "不记录"}])

    assert not snapshot_dir.exists()
    assert not last_snapshot.exists()

    response_on = _ns(
        choices=[_ns(message=_ns(content="开启", tool_calls=[]))], usage=None
    )
    config = LLMConfig(api_key="test", model="deepseek-v4-flash")
    provider_on = LLMProvider(config, payload_snapshot_enabled=True)
    provider_on._client = _Client(response_on)
    await provider_on.complete([{"role": "user", "content": "记录我"}])

    files = list(snapshot_dir.glob("*.json"))
    assert len(files) == 1
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    assert payload["messages"][0]["content"] == "记录我"
    assert json.loads(last_snapshot.read_text(encoding="utf-8")) == payload


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


@pytest.mark.asyncio
async def test_deepseek_strategy_maps_thinking_config_and_effort() -> None:
    response = _ns(
        choices=[_ns(message=_ns(content="完成", reasoning_content=None, tool_calls=[]))],
        usage=None,
    )
    client = _Client(response)
    provider = _provider(
        client,
        extra_body={"enable_thinking": True, "reasoning_effort": "xhigh"},
    )

    await provider.complete([{"role": "user", "content": "分析问题"}])

    request = client.completions.calls[-1]
    assert request["extra_body"] == {"thinking": {"type": "enabled"}}
    assert request["reasoning_effort"] == "max"


@pytest.mark.asyncio
async def test_deepseek_strategy_disables_thinking_for_one_call() -> None:
    response = _ns(
        choices=[_ns(message=_ns(content="完成", reasoning_content=None, tool_calls=[]))],
        usage=None,
    )
    client = _Client(response)
    provider = _provider(
        client,
        extra_body={
            "enable_thinking": True,
            "thinking": {"type": "enabled"},
            "reasoning_effort": "high",
        },
    )

    await provider.chat(
        [{"role": "user", "content": "简单回答"}],
        disable_thinking=True,
    )

    request = client.completions.calls[-1]
    assert request["extra_body"] == {"thinking": {"type": "disabled"}}
    assert "reasoning_effort" not in request


@pytest.mark.asyncio
async def test_deepseek_strategy_patches_dirty_assistant_history() -> None:
    response = _ns(
        choices=[_ns(message=_ns(content="完成", reasoning_content=None, tool_calls=[]))],
        usage=None,
    )
    client = _Client(response)
    provider = _provider(client, extra_body={"enable_thinking": True})
    messages = [
        {"role": "user", "content": "第一次"},
        {"role": "assistant", "content": "旧回复"},
        {"role": "assistant", "content": "已有", "reasoning_content": "保留我"},
        {"role": "user", "content": "继续"},
    ]

    await provider.complete(messages)

    sent_messages = client.completions.calls[-1]["messages"]
    assert sent_messages[1]["reasoning_content"] == ""
    assert sent_messages[2]["reasoning_content"] == "保留我"
    assert "reasoning_content" not in messages[1]


@pytest.mark.asyncio
async def test_deepseek_strategy_strips_image_blocks_but_keeps_text() -> None:
    response = _ns(
        choices=[_ns(message=_ns(content="完成", reasoning_content=None, tool_calls=[]))],
        usage=None,
    )
    client = _Client(response)
    provider = _provider(client)

    await provider.complete(
        [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,AAAA"},
                    },
                    {"type": "text", "text": "看看这张图"},
                ],
            }
        ]
    )

    content = client.completions.calls[-1]["messages"][0]["content"]
    assert content == (
        "看看这张图\n"
        "[已移除 1 个 image_url 图片块：DeepSeek 当前接口只接受文本消息。]"
    )


@pytest.mark.asyncio
async def test_deepseek_tool_call_preserves_reasoning_in_provider_fields() -> None:
    response = _ns(
        choices=[
            _ns(
                message=_ns(
                    content=None,
                    reasoning_content="先查资料",
                    tool_calls=[
                        _ns(
                            id="call-1",
                            function=_ns(name="search", arguments='{"q":"x"}'),
                        )
                    ],
                )
            )
        ],
        usage=None,
    )
    provider = _provider(_Client(response))

    result = await provider.complete(
        [{"role": "user", "content": "查询"}],
        tools=[{"type": "function", "function": {"name": "search"}}],
    )

    assert result.thinking == "先查资料"
    assert result.provider_fields == {"reasoning_content": "先查资料"}


@pytest.mark.asyncio
async def test_deepseek_tool_call_fills_empty_reasoning_when_not_disabled() -> None:
    response = _ns(
        choices=[
            _ns(
                message=_ns(
                    content=None,
                    reasoning_content=None,
                    tool_calls=[
                        _ns(
                            id="call-1",
                            function=_ns(name="search", arguments="{}"),
                        )
                    ],
                )
            )
        ],
        usage=None,
    )
    provider = _provider(_Client(response))

    result = await provider.complete(
        [{"role": "user", "content": "查询"}],
        tools=[{"type": "function"}],
    )

    assert result.provider_fields == {"reasoning_content": ""}


@pytest.mark.asyncio
async def test_native_deepseek_thinking_config_patches_history() -> None:
    response = _ns(
        choices=[
            _ns(message=_ns(content="完成", reasoning_content=None, tool_calls=[]))
        ],
        usage=None,
    )
    client = _Client(response)
    provider = _provider(
        client, extra_body={"thinking": {"type": "enabled"}}
    )

    await provider.complete([{"role": "assistant", "content": "旧回复"}])

    assert client.completions.calls[-1]["messages"][0]["reasoning_content"] == ""


@pytest.mark.asyncio
async def test_disabled_thinking_tool_call_does_not_fill_reasoning() -> None:
    response = _ns(
        choices=[
            _ns(
                message=_ns(
                    content=None,
                    reasoning_content=None,
                    tool_calls=[
                        _ns(
                            id="call-1",
                            function=_ns(name="search", arguments="{}"),
                        )
                    ],
                )
            )
        ],
        usage=None,
    )
    provider = _provider(
        _Client(response), extra_body={"thinking": {"type": "disabled"}}
    )

    result = await provider.complete(
        [{"role": "user", "content": "查询"}],
        tools=[{"type": "function"}],
    )

    assert result.provider_fields == {}


@pytest.mark.asyncio
async def test_stream_stops_forwarding_temporary_deltas_after_tool_call() -> None:
    stream = _Stream(
        _ns(
            choices=[_ns(delta=_ns(reasoning_content="先想", content=None, tool_calls=[]))],
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
                                id="call-1",
                                function=_ns(name="search", arguments="{}"),
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
                        reasoning_content="不应转发",
                        content="临时文本",
                        tool_calls=[],
                    )
                )
            ],
            usage=None,
        ),
    )
    provider = _provider(_Client(stream))
    deltas: list[dict[str, str]] = []

    async def receive_delta(delta: dict[str, str]) -> None:
        deltas.append(delta)

    result = await provider.chat(
        [{"role": "user", "content": "查询"}],
        tools=[{"type": "function"}],
        on_content_delta=receive_delta,
    )

    assert deltas == [{"thinking_delta": "先想"}]
    assert result.thinking == "先想不应转发"
    assert result.content == "临时文本"
    assert result.provider_fields == {"reasoning_content": "先想不应转发"}
