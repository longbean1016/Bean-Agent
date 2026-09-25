"""环境事实在受控进程内验证，缓存隔离且失败不自动提升权限。"""

import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sandbox import environment_probe
from sandbox.errors import SandboxUnavailable
from sandbox.runtime import SandboxRunResult
from sandbox.shell_environment import ShellEnvironmentCache
from tools.shell import ShellTool


def _result():
    return SandboxRunResult(json.dumps({"os": "Windows", "shell": "cmd.exe", "cwd": "project", "python": None, "curl": None}).encode(), b"", 0, False)


@pytest.mark.asyncio
async def test_cache_scopes_expiration_and_capacity(monkeypatch):
    now = [1.0]
    monkeypatch.setattr("sandbox.shell_environment.time.monotonic", lambda: now[0])
    cache = ShellEnvironmentCache(ttl=10, capacity=2)
    probe = AsyncMock(return_value=_result())
    first = await cache.get(("session-a", "cwd-a", "read-only"), probe)
    assert await cache.get(("session-a", "cwd-a", "read-only"), probe) is first
    assert probe.await_count == 1
    await cache.get(("session-a", "cwd-b", "read-only"), probe)
    await cache.get(("session-b", "cwd-a", "read-only"), probe)
    assert len(cache._cache) == 2
    await cache.get(("session-a", "cwd-a", "read-only"), probe)
    now[0] = 12
    await cache.get(("session-a", "cwd-a", "read-only"), probe)
    assert probe.await_count == 5


@pytest.mark.asyncio
async def test_restricted_probe_failure_never_falls_back_to_host(tmp_path, monkeypatch):
    policy = SimpleNamespace(cwd=tmp_path, mode="read-only", temp_dir=tmp_path)
    guard = SimpleNamespace(policy=lambda key: policy)
    runtime = SimpleNamespace(run=AsyncMock(side_effect=SandboxUnavailable("private details")))
    host = AsyncMock()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", host)
    tool = ShellTool(sandbox_guard=guard, sandbox_runtime=runtime)
    text = await tool.environment_context("session")
    assert "不可用" in text and "private details" not in text
    host.assert_not_awaited()
    assert runtime.run.call_args.args[0] is policy
    assert runtime.run.call_args.kwargs["cwd"] == tmp_path
    assert "execution_mode" not in runtime.run.call_args.kwargs
    await tool.environment_context("session")
    assert runtime.run.await_count == 1


def test_project_environment_precedes_path_and_application(tmp_path, monkeypatch):
    project = tmp_path / "project with spaces"
    venv_python = project / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    venv_python.parent.mkdir(parents=True)
    venv_python.touch()
    monkeypatch.chdir(project)
    monkeypatch.setattr(environment_probe.shutil, "which", lambda name: "inherited-python" if name == "python" else None)
    calls = []
    def version(path, kind):
        calls.append(path)
        return "python 3.12.0" if path == venv_python else None
    monkeypatch.setattr(environment_probe, "_version", version)
    result = environment_probe.discover()
    assert result["python"]["path"] == str(venv_python)
    assert result["python"]["source"] == "项目虚拟环境"
    assert calls == [venv_python]


@pytest.mark.skipif(os.name != "nt", reason="Windows 应用别名")
def test_windows_apps_alias_is_not_executed(monkeypatch):
    launch = AsyncMock()
    monkeypatch.setattr(environment_probe.subprocess, "Popen", launch)
    assert environment_probe._version(Path(r"C:\Users\test\AppData\Local\Microsoft\WindowsApps\python.exe"), "python") is None
    launch.assert_not_called()


def test_real_python_probe_uses_isolated_mode_without_site(tmp_path, monkeypatch):
    marker = tmp_path / "unexpected.txt"
    (tmp_path / "sitecustomize.py").write_text(f"open({str(marker)!r}, 'w').close()", encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    assert environment_probe._version(Path(sys.executable), "python").startswith("python ")
    assert not marker.exists()


@pytest.mark.asyncio
async def test_standalone_environment_matches_execution_cwd(tmp_path):
    tool = ShellTool(working_dir=tmp_path)
    text = await tool.environment_context("standalone")
    assert json.dumps(str(tmp_path), ensure_ascii=False) in text
    assert "已验证 python" in text
    assert "cmd.exe" in text if os.name == "nt" else "/bin/sh" in text


@pytest.mark.asyncio
async def test_probe_cancellation_is_not_cached():
    cache = ShellEnvironmentCache()
    with pytest.raises(asyncio.CancelledError):
        await cache.get(("session",), AsyncMock(side_effect=asyncio.CancelledError()))
    assert not cache._cache


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [SandboxRunResult(b"", b"", -1, True), SandboxRunResult(b"x" * 8193, b"", 0, False), SandboxRunResult(b"[]", b"", 0, False)])
async def test_invalid_or_timeout_probe_degrades_safely(result):
    snapshot = await ShellEnvironmentCache().get(("session",), AsyncMock(return_value=result))
    assert "探测不可用" in snapshot.text
    assert "已验证" not in snapshot.text


@pytest.mark.asyncio
async def test_policy_and_environment_changes_invalidate_cache(tmp_path, monkeypatch):
    policy = SimpleNamespace(cwd=tmp_path, mode="read-only", temp_dir=tmp_path)
    runtime = SimpleNamespace(run=AsyncMock(return_value=_result()))
    tool = ShellTool(sandbox_guard=SimpleNamespace(policy=lambda key: policy), sandbox_runtime=runtime)
    await tool.environment_context("a")
    await tool.environment_context("a")
    assert runtime.run.await_count == 1
    policy.mode = "workspace-write"
    await tool.environment_context("a")
    monkeypatch.setenv("PATH", os.environ.get("PATH", "") + os.pathsep + "test-change")
    await tool.environment_context("a")
    await tool.environment_context("b")
    assert runtime.run.await_count == 4


def test_unavailable_path_python_uses_explicitly_labeled_fallback(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.setattr(environment_probe.shutil, "which", lambda name: None)
    monkeypatch.setattr(environment_probe, "_version", lambda path, kind: "python 3.12.0" if str(path) == sys.executable else None)
    result = environment_probe.discover()
    assert result["python"]["path"] == sys.executable
    assert "应用备用" in result["python"]["source"]
    assert result["curl"] is None
