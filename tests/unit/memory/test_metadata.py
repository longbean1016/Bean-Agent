"""记忆元数据持久化前的序列化校验。"""

import json

import pytest

from memory.metadata import serialize_memory_metadata


def test_valid_metadata_keeps_nested_business_fields() -> None:
    metadata = {"场景": "测试", "steps": ["检查"], "nested": {"enabled": True, "optional": None, "weight": 1.5}}
    assert json.loads(serialize_memory_metadata(metadata)) == metadata


@pytest.mark.parametrize("metadata", [None, [], "text", {"object": object()}, {"nan": float("nan")}, {"inf": float("inf")}])
def test_invalid_metadata_is_rejected_without_string_fallback(metadata) -> None:
    with pytest.raises(ValueError, match="记忆元数据"):
        serialize_memory_metadata(metadata)


def test_cyclic_metadata_is_rejected_without_echoing_content() -> None:
    metadata = {"private": "不应回显的测试内容"}
    metadata["cycle"] = metadata
    with pytest.raises(ValueError) as error:
        serialize_memory_metadata(metadata)
    assert "不应回显" not in str(error.value)
