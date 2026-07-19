"""离线测试使用的最小 stdio MCP Server。"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def main() -> None:
    log_path = Path(os.environ["BEANAGENT_MCP_TEST_LOG"])
    for line in sys.stdin:
        request = json.loads(line)
        with log_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(request, ensure_ascii=False) + "\n")
        method = request.get("method")
        if "id" not in request:
            continue
        if method == "initialize":
            result = {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "test", "version": "1"},
            }
        elif method == "tools/list":
            result = {
                "tools": [
                    {
                        "name": "echo",
                        "description": "回显文本",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"text": {"type": "string"}},
                            "required": ["text"],
                        },
                    }
                ]
            }
        elif method == "tools/call":
            text = str(request.get("params", {}).get("arguments", {}).get("text", ""))
            result = {"content": [{"type": "text", "text": f"echo:{text}"}]}
        else:
            response = {
                "jsonrpc": "2.0",
                "id": request["id"],
                "error": {"code": -32601, "message": "unknown"},
            }
            print(json.dumps(response), flush=True)
            continue
        print(
            json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}),
            flush=True,
        )


if __name__ == "__main__":
    main()
