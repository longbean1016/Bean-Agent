"""环境摘要经动态上下文进入模型，不修改静态前缀或工具存储协议。"""

from unittest.mock import AsyncMock
from types import SimpleNamespace

import pytest

from agent.event_bus import EventBus
from agent.message_bus import InboundMessage
from agent.pipeline import Pipeline
from agent.prompt_assembler import MessageEnvelopeBuilder, PromptAssembler
from agent.prompt_block import SystemPromptBuilder, default_prompt_blocks
from agent.provider import LLMResponse
from tools.registry import ToolRegistry
from tools.shell import ShellTool


@pytest.mark.asyncio
async def test_pipeline_injects_environment_in_dynamic_frame(tmp_path):
    provider = SimpleNamespace(chat=AsyncMock(return_value=LLMResponse("完成")))
    registry = ToolRegistry()
    registry.register(ShellTool(working_dir=tmp_path))
    loader = AsyncMock(return_value="实际 Shell: cmd.exe；已验证 Python: test-python")
    pipeline = Pipeline(provider, registry, EventBus(), PromptAssembler(SystemPromptBuilder(default_prompt_blocks()), MessageEnvelopeBuilder()), workspace=str(tmp_path), shell_environment_loader=loader)
    await pipeline.process(InboundMessage("web", "user", "test", "查看环境"), turn_id="turn-test")
    loader.assert_awaited_once_with("web:test")
    messages = provider.chat.call_args.args[0]
    assert "test-python" not in messages[0]["content"]
    assert any("test-python" in str(message.get("content")) for message in messages[1:])


@pytest.mark.asyncio
async def test_pipeline_without_shell_does_not_probe(tmp_path):
    provider = SimpleNamespace(chat=AsyncMock(return_value=LLMResponse("完成")))
    loader = AsyncMock()
    pipeline = Pipeline(provider, ToolRegistry(), EventBus(), PromptAssembler(SystemPromptBuilder(default_prompt_blocks()), MessageEnvelopeBuilder()), workspace=str(tmp_path), shell_environment_loader=loader)
    await pipeline.process(InboundMessage("web", "user", "test", "你好"), turn_id="turn-test")
    loader.assert_not_awaited()
