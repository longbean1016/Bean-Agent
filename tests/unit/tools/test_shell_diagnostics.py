"""失败分类必须基于命令与证据，不能全局猜测输出含义。"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sandbox.runtime import SandboxRunResult
from tools.shell import ShellTool, _validate_command
from tools.shell_diagnostics import allows_permission_retry, diagnose
from agent.pipeline import _classify_tool_result
from tools.base import ToolResult


@pytest.mark.parametrize("command", ["curl.exe", '"C:\\Program Files\\curl.exe"', "curl"])
def test_network_executable_forms_share_validation(command):
    assert "显式提供" in _validate_command(f'{command} -s "example.com"', allow_network=True, restricted_dir=None)
    assert "禁止访问" in _validate_command(f'{command} "http://127.0.0.1/"', allow_network=True, restricted_dir=None)
    assert "禁止网络" in _validate_command(f'{command} "https://example.com/"', allow_network=False, restricted_dir=None)
    assert "禁止上传" in _validate_command(f'{command} -T secret.txt "https://example.com/"', allow_network=True, restricted_dir=None)
    assert _validate_command(f'{command} -sS "https://example.com/?a=1&b=2"', allow_network=True, restricted_dir=None) is None


@pytest.mark.parametrize("command", ['del "file.txt"', 'erase "file.txt"'])
@pytest.mark.parametrize("output", ["file.txt\nAccess is denied.\n", "file.txt\r\n拒绝访问。\r\n"])
def test_zero_exit_delete_denial(command, output):
    diagnostic = diagnose(command, 0, False, output, "", windows=True)
    assert diagnostic.code == "shell_partial_failure"
    result = ToolResult(json.dumps({"exit_code": 0, "error": diagnostic.message}))
    assert _classify_tool_result(result) == ("error", "tool_error", 0)


@pytest.mark.parametrize("command", ['echo "Access is denied."', 'type log.txt', 'del a & echo "Access is denied."', "del 'a & echo denial'", 'del a | more', 'del %TARGET%', '"del.exe" a'])
def test_successful_output_not_globally_classified_as_failure(command):
    assert diagnose(command, 0, False, "error\nAccess is denied.\n", "warning", windows=True) is None


def test_curl_tls_failure_keeps_uncertainty_and_does_not_classify_pipeline():
    command = 'curl.exe -s "https://example.com/?a=1&b=2"'
    assert diagnose(command, 35, False, "", "", windows=True).code == "shell_network_failure"
    assert diagnose(command + ' | python -c "print(1)"', 35, False, "", "", windows=True).code == "shell_exit_nonzero"
    assert diagnose("unknown", -1, True, "partial", "", windows=True).code == "shell_timeout"


@pytest.mark.parametrize("command", ['del file.txt', 'move a b', 'rd folder', 'powershell -Command Remove-Item a', 'echo ok & del a', 'python script.py'])
def test_destructive_or_opaque_commands_cannot_be_replayed(command):
    assert not allows_permission_retry(command)


def test_existing_single_command_approval_remains_available():
    assert allows_permission_retry('type "file.txt"')
    assert allows_permission_retry('echo approved>"file.txt"')


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_code", [0, 1])
async def test_delete_failure_is_reported_without_automatic_retry(tmp_path, exit_code):
    policy = SimpleNamespace(cwd=tmp_path, mode="read-only")
    guard = SimpleNamespace(policy=lambda key: policy, authorize_shell_retry=AsyncMock())
    tool = ShellTool(sandbox_guard=guard, sandbox_runtime=object())
    tool._sandbox_shell = SimpleNamespace(execute=AsyncMock(return_value=SandboxRunResult(b"Access is denied.\n", b"", exit_code, False)))
    result = json.loads(await tool.execute(command="del file.txt", description="删除文件", session_key="test", turn_id="turn", call_id="call"))
    assert result["exit_code"] == exit_code
    assert result["diagnostic_message"]
    if exit_code == 0:
        assert result["error"]
    else:
        assert _classify_tool_result(ToolResult(json.dumps(result))) == ("error", "tool_exit_nonzero", exit_code)
    guard.authorize_shell_retry.assert_not_awaited()
    assert tool._sandbox_shell.execute.await_count == 1
