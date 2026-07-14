"""配置模型与加载器的单元测试。"""

from __future__ import annotations

from dataclasses import fields
from pathlib import Path

import pytest

from agent.config import load_config
from agent.config_models import Config


def test_config_defaults_and_nested_instances_are_independent() -> None:
    first = Config()
    second = Config()

    assert [item.name for item in fields(Config)] == [
        "llm",
        "memory",
        "session",
        "agent",
        "channels",
    ]
    assert first.llm.model == "deepseek-v4-flash"
    assert first.llm.max_tokens == 8192
    assert first.memory.embedding.dimensions == 1024
    assert first.session.history_window == 40
    assert first.channels.chat.port == 6322
    assert first.channels.chat.channel_name == "web"

    first.memory.embedding.model = "changed"
    assert second.memory.embedding.model == "text-embedding-v3"


def test_load_config_maps_nested_values_and_resolves_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BEAN_LLM_KEY", "llm-secret")
    monkeypatch.setenv("BEAN_EMBEDDING_KEY", "embedding-secret")
    config_path = tmp_path / "bean.toml"
    config_path.write_text(
        """
[llm]
provider = "qwen"
model = "qwen-plus"
api_key = "${BEAN_LLM_KEY}"
max_tokens = 2048
max_iterations = 6
system_prompt = "测试助手"
request_timeout_s = 45.5
enable_thinking = true
reasoning_effort = "xhigh"

[memory]
enabled = true
engine_name = "local"

[memory.embedding]
model = "text-embedding-v3"
api_key = "${BEAN_EMBEDDING_KEY}"
base_url = "https://embedding.example/v1"
dimensions = 768

[memory.optimizer]
enabled = false
interval_seconds = 120
merge_max_tokens = 8000
self_update_max_tokens = 1000
step_delay_seconds = 3

[memory.retrieval]
hotness_alpha = 0.3
half_life_days = 10.0
rrf_k = 50
keyword_rrf_weight = 0.4

[memory.dedup]
supersede_threshold = 0.8
event_dedup_threshold = 0.85
event_dedup_window_days = 5

[session]
history_window = 25

[agent]
workdir = "runtime"

[channels.chat]
enabled = false
host = "0.0.0.0"
port = 8000
""".strip(),
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.llm.api_key == "llm-secret"
    assert config.llm.base_url == "https://dashscope.aliyuncs.com/compatible-mode/v1"
    assert config.llm.request_timeout_s == 45.5
    assert config.llm.extra_body == {
        "enable_thinking": True,
        "reasoning_effort": "xhigh",
    }
    assert config.memory.embedding.api_key == "embedding-secret"
    assert config.memory.embedding.dimensions == 768
    assert config.memory.optimizer.enabled is False
    assert config.memory.retrieval.rrf_k == 50
    assert config.memory.dedup.event_dedup_window_days == 5
    assert config.session.history_window == 25
    assert config.agent.workdir == "runtime"
    assert config.channels.chat.host == "0.0.0.0"
    assert config.channels.chat.port == 8000
    assert config.channels.chat.channel_name == "web"


def test_explicit_base_url_overrides_provider_preset(tmp_path: Path) -> None:
    config_path = tmp_path / "bean.toml"
    config_path.write_text(
        '[llm]\nprovider = "deepseek"\nbase_url = "https://proxy.example/v1"\n',
        encoding="utf-8",
    )

    assert load_config(config_path).llm.base_url == "https://proxy.example/v1"


def test_missing_provider_and_base_url_keep_none(tmp_path: Path) -> None:
    config_path = tmp_path / "bean.toml"
    config_path.write_text("[llm]\n", encoding="utf-8")

    assert load_config(config_path).llm.base_url is None


def test_unset_environment_placeholder_is_preserved(tmp_path: Path) -> None:
    config_path = tmp_path / "bean.toml"
    config_path.write_text(
        '[llm]\napi_key = "${BEAN_MISSING_KEY}"\n', encoding="utf-8"
    )

    assert load_config(config_path).llm.api_key == "${BEAN_MISSING_KEY}"


def test_load_config_rejects_non_toml_file(tmp_path: Path) -> None:
    config_path = tmp_path / "bean.json"
    config_path.write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="主配置仅支持 TOML"):
        load_config(config_path)


def test_config_template_uses_project_runtime_defaults() -> None:
    config = load_config("config.example.toml")

    assert config.llm.model == "deepseek-v4-flash"
    assert config.llm.max_tokens == 8192
    assert config.llm.max_iterations == 40
    assert config.llm.extra_body == {"enable_thinking": True}
    assert config.memory.embedding.model == "text-embedding-v3"
    assert (
        config.memory.embedding.base_url
        == "https://dashscope.aliyuncs.com/compatible-mode/v1"
    )
