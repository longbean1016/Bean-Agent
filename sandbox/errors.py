"""沙箱领域错误。"""

from __future__ import annotations


class SandboxError(RuntimeError):
    """沙箱无法安全完成请求。"""


class SandboxAccessDenied(SandboxError):
    """当前会话策略拒绝执行。"""


class SandboxUnavailable(SandboxError):
    """当前平台或运行时无法提供声明的隔离能力。"""


class ApprovalUnavailable(SandboxAccessDenied):
    """越权请求没有可用的用户审批界面。"""


__all__ = [
    "ApprovalUnavailable",
    "SandboxAccessDenied",
    "SandboxError",
    "SandboxUnavailable",
]
