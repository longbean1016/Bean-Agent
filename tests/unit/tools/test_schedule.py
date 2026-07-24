"""定时任务工具的 tier 选择契约与字段边界测试。"""

from __future__ import annotations

import pytest

from proactive.store import ProactiveStore
from tools.schedule import ScheduleTool


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tier", "message", "prompt", "expected"),
    [
        ("instant", "", "提醒用户午休", "instant 必须提供最终提醒文本 message"),
        ("instant", "午休时间到了", "提醒用户午休", "instant 不应提供 prompt"),
        ("soft", "", "", "soft 必须提供可独立执行的任务指令 prompt"),
        ("soft", "天气提醒", "查询实时天气", "soft 不应提供 message"),
    ],
)
async def test_schedule_rejects_cross_tier_content_fields(
    tmp_path,
    tier: str,
    message: str,
    prompt: str,
    expected: str,
) -> None:
    store = ProactiveStore(tmp_path / "proactive.db")
    tool = ScheduleTool(store)

    result = await tool.execute(
        session_key="web:a",
        tier=tier,
        trigger="after",
        when="10m",
        message=message,
        prompt=prompt,
        request_time="2026-07-24T12:00:00+08:00",
    )

    assert expected in result
    assert store.list_jobs("web:a") == []
    store.close()


def test_schedule_schema_explains_creation_and_execution_boundaries(tmp_path) -> None:
    store = ProactiveStore(tmp_path / "proactive.db")
    tool = ScheduleTool(store)

    assert "最终提醒文本" in tool.description
    assert "到期后" in tool.description
    prompt_description = tool.parameters["properties"]["prompt"]["description"]
    assert "不要在创建任务时提前执行" in prompt_description
    assert "不得编造" in prompt_description
    store.close()
