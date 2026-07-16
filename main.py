"""BeanAgent FastAPI 服务启动入口。"""

from __future__ import annotations

import argparse
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from agent.config import load_config
from bootstrap.app import build_core_runtime, create_fastapi_app


def default_workspace() -> Path:
    """运行数据默认与源码分离，命令行可整体覆盖。"""

    return Path.home() / ".beanagent" / "workspace"


def build_application(config_path: str | Path, workspace: str | Path) -> FastAPI:
    """加载一次配置并逐层组装应用，便于 CLI 与部署服务器复用。"""

    config = load_config(config_path)
    core = build_core_runtime(config, Path(workspace))
    return create_fastapi_app(core)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="启动 BeanAgent 网页对话服务")
    parser.add_argument("--config", default="config.toml", help="TOML 配置文件路径")
    parser.add_argument("--workspace", default=str(default_workspace()), help="Session 与记忆运行数据目录")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    app = build_application(args.config, args.workspace)
    config = app.state.core_runtime.config
    uvicorn.run(
        app,
        host=config.channels.chat.host,
        port=config.channels.chat.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()


__all__ = ["build_application", "default_workspace", "main"]
