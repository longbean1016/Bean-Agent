"""本地 HTTP MCP 测试服务，用于验证 BeanAgent 的远程连接闭环。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class McpHandler(BaseHTTPRequestHandler):
    server_version = "BeanAgentHttpMcpTest/1.0"

    def do_POST(self) -> None:  # noqa: N802 - 标准库处理器要求的方法名
        if self.path.split("?", 1)[0] != "/mcp":
            self.send_error(404, "MCP endpoint not found")
            return
        try:
            size = int(self.headers.get("content-length", "0"))
            request = json.loads(self.rfile.read(size).decode("utf-8"))
            response = self._handle_json_rpc(request)
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            self._write_json({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}, 400)
            return
        if response is None:
            self.send_response(204)
            self.end_headers()
            return
        self._write_json(response)

    def do_OPTIONS(self) -> None:  # noqa: N802 - 兼容浏览器调试请求
        self.send_response(204)
        self._write_cors_headers()
        self.end_headers()

    def _handle_json_rpc(self, request: dict[str, Any]) -> dict[str, Any] | None:
        method = request.get("method")
        request_id = request.get("id")
        if request_id is None and method == "notifications/initialized":
            return None
        if method == "initialize":
            result: dict[str, Any] = {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "http-test-server", "version": "1.0.0"},
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {
                "tools": [
                    {
                        "name": "echo",
                        "description": "回显输入文本",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"text": {"type": "string"}},
                            "required": ["text"],
                        },
                    },
                    {
                        "name": "add",
                        "description": "计算两个数字的和",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"left": {"type": "number"}, "right": {"type": "number"}},
                            "required": ["left", "right"],
                        },
                    },
                ]
            }
        elif method == "tools/call":
            params = request.get("params") if isinstance(request.get("params"), dict) else {}
            tool_name = params.get("name")
            arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
            if tool_name == "echo":
                text = str(arguments.get("text") or "")
            elif tool_name == "add":
                text = str(float(arguments.get("left", 0)) + float(arguments.get("right", 0)))
            else:
                return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "Unknown tool"}}
            result = {"content": [{"type": "text", "text": text}]}
        else:
            return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": f"Unknown method: {method}"}}
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def _write_json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self._write_cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def _write_cors_headers(self) -> None:
        self.send_header("access-control-allow-origin", "*")
        self.send_header("access-control-allow-headers", "content-type, authorization, mcp-session-id")
        self.send_header("access-control-allow-methods", "GET, POST, OPTIONS")

    def log_message(self, format: str, *args: object) -> None:
        print(f"[http-mcp] {format % args}")


def main() -> None:
    parser = argparse.ArgumentParser(description="启动本地 HTTP MCP 测试服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), McpHandler)
    print(f"HTTP MCP test server listening on http://{args.host}:{args.port}/mcp")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
