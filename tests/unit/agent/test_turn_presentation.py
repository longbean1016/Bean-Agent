"""展示投影只派生事实，不影响模型消息或工具存储。"""

import json

from agent.turn_presentation import TurnPresentation


def test_order_and_final_boundary() -> None:
    view = TurnPresentation("2026-09-25T10:00:00Z")
    view.add_memory({"query": "夜跑", "items": [{"id": "m1", "summary": "晚上跑步"}]}, 20)
    thinking = view.append_text(1, "thinking", "先查")
    assert view.append_text(1, "thinking", "天气") == thinking
    view.append_text(1, "text", "查一下")
    view.resolve_response(1, "查一下", "先查天气", final=False)
    view.add_tool("c1")
    view.append_text(2, "thinking", "整理")
    view.append_text(2, "text", "适合")
    assert not view.data.get("final")
    view.resolve_response(2, "适合跑步", "整理", final=True)
    parts = view.snapshot()["parts"]
    assert [p["kind"] for p in parts] == ["memory", "thinking", "text", "tool", "thinking", "answer"]
    assert parts[-1]["text"] == "适合跑步"
    assert view.data["final"]


def test_snapshots_are_isolated_and_empty_auto_memory_is_absent() -> None:
    view = TurnPresentation("start")
    view.add_memory({"query": "empty", "items": []}, 5)
    snapshot = view.snapshot()
    view.append_text(1, "text", "内容")
    assert snapshot["parts"] == []
    assert view.data["parts"]


def test_explicit_memory_summary_is_extracted_before_truncation_without_internal_fields() -> None:
    view = TurnPresentation("start")
    view.add_tool("recall")
    result = json.dumps({"items": [{"id": "m1", "summary": "跑步" * 400, "signals": {"secret": "private"}}], "trace": {"internal": True}})
    assert view.complete_memory_tool("recall", result)
    payload = view.snapshot()["parts"][0]["memory_result"]
    assert payload["count"] == 1
    assert set(payload["items"][0]) == {"id", "summary"}
    assert "private" not in json.dumps(payload)
    assert not view.complete_memory_tool("recall", "broken json")
    assert view.complete_memory_tool("recall", '{"items":[]}')
    assert view.data["parts"][0]["memory_result"]["count"] == 0
