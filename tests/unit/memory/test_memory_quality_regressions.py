"""记忆 Prompt 质量场景的脚本化回归矩阵。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from memory.events import TurnIngested
from memory.implicit_extractor import ImplicitLongTermExtractor
from memory.post_response_worker import PostResponseMemoryWorker
from memory.query_rewriter import QueryRewriter


class ScenarioProvider:
    """用确定性规则模拟遵守 Prompt 的 LLM，验证后续解析与边界。"""

    async def complete(self, messages, tools=None):
        prompt = messages[0]["content"]
        if "长期记忆提取专家" in prompt:
            conversation = prompt.split("【待处理对话】", 1)[1].split("只返回合法 JSON", 1)[0]
            if "我在互联网公司做产品经理" in conversation:
                data = {"profile": [{"summary": "用户在互联网公司做产品经理", "category": "personal_fact"}], "preference": [], "procedure": []}
            elif "以后讲复杂问题" in conversation:
                data = {"profile": [], "preference": [{"summary": "讲解复杂问题时使用贯穿示例"}], "procedure": []}
            elif "以后查耳机" in conversation:
                data = {"profile": [], "preference": [], "procedure": [{"summary": "查询耳机时先查看评测和社区讨论", "steps": ["查看评测", "查看社区讨论"]}]}
            else:
                data = {"profile": [], "preference": [], "procedure": []}
            return SimpleNamespace(content=json.dumps(data, ensure_ascii=False))
        if "记忆检索决策器" in prompt:
            current = prompt.split("当前用户消息：", 1)[1].split("规则：", 1)[0]
            retrieve = any(text in current for text in ("你还记得", "都有哪些"))
            decision = "RETRIEVE" if retrieve else "NO_RETRIEVE"
            return SimpleNamespace(content=f"<decision>{decision}</decision><history_query>用户历史</history_query>")
        if "只输出一行检索 query" in prompt:
            return SimpleNamespace(content="none")
        if "受影响的行为主题" in prompt:
            explicit = "下载流程错了，不要再用" in prompt
            return SimpleNamespace(content='["下载流程"]' if explicit else "[]")
        return SimpleNamespace(content='["old-rule"]')


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("conversation", "counts"),
    [
        ("[user] 我在互联网公司做产品经理", (1, 0, 0)),
        ("[user] 你还记得我住哪里吗", (0, 0, 0)),
        ("[user] 在赶代码\n[assistant] 建议每隔 45 分钟喝水", (0, 0, 0)),
        ("[user] 今晚想找一家日料店", (0, 0, 0)),
        ("[user] 以后讲复杂问题给我贯穿始终的例子", (0, 1, 0)),
        ("[user] 以后查耳机先看评测和社区讨论", (0, 0, 1)),
    ],
)
async def test_implicit_memory_quality_scenarios(conversation: str, counts: tuple[int, int, int]) -> None:
    result = await ImplicitLongTermExtractor(ScenarioProvider()).extract(conversation)
    assert (len(result.profile), len(result.preference), len(result.procedure)) == counts


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "needs_history"),
    [
        ("TCP 和 UDP 有什么区别", False),
        ("以后回答先给结论", False),
        ("你还记得我的设备型号吗", True),
        ("我历史上都有哪些购买记录", True),
    ],
)
async def test_query_gate_quality_scenarios(message: str, needs_history: bool) -> None:
    decision = await QueryRewriter(ScenarioProvider()).decide(message)
    assert decision.needs_episodic is needs_history


class Memorizer:
    def __init__(self): self.superseded = []
    def supersede_batch(self, ids): self.superseded.extend(ids)


class Retriever:
    async def retrieve(self, query, memory_types=None):
        return [{"id": "old-rule", "summary": "旧下载流程", "score": 0.95}]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "should_supersede"),
    [
        ("之前的下载流程错了，不要再用", True),
        ("你的下载流程是什么？", False),
        ("我之前用过下载脚本", False),
        ("也许下载流程可能不太对", False),
    ],
)
async def test_invalidation_quality_scenarios(message: str, should_supersede: bool) -> None:
    memorizer = Memorizer()
    worker = PostResponseMemoryWorker(memorizer, Retriever(), ScenarioProvider())
    await worker.handle(TurnIngested("web:c", "web", "c", message, "回答", [], "web:c@turn"))
    assert bool(memorizer.superseded) is should_supersede
