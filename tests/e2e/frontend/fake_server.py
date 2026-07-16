"""Batch 6 Playwright 使用的离线 HTTP/WebSocket Fake 服务。"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

import uvicorn
from fastapi import FastAPI, Query, Request, WebSocket
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.websockets import WebSocketDisconnect

ROOT = Path(__file__).resolve().parents[3]
STATIC = ROOT / "static" / "chat"
UPLOADS = Path(tempfile.gettempdir()) / "beanagent-playwright-uploads"
UPLOADS.mkdir(parents=True, exist_ok=True)

app = FastAPI()
app.mount("/assets", StaticFiles(directory=STATIC), name="assets")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/chat/sessions")
def sessions() -> dict[str, object]:
    return {
        "items": [{
            "key": "web:history",
            "created_at": "2026-07-16T08:00:00+08:00",
            "updated_at": "2026-07-16T09:00:00+08:00",
            "message_count": 2,
            "first_message_content": "历史问题",
        }],
        "total": 1,
    }


@app.get("/api/chat/sessions/{session_key:path}/messages")
def messages(session_key: str) -> dict[str, object]:
    if session_key == "web:history":
        return {
            "items": [
                {"id": "web:history:0", "role": "user", "content": "历史问题", "turn_id": "history-turn", "timestamp": "2026-07-16T08:00:00+08:00"},
                {"id": "web:history:1", "role": "assistant", "content": "历史回答", "turn_id": "history-turn", "timestamp": "2026-07-16T08:00:01+08:00"},
            ],
            "total": 2,
            "session_id": session_key,
        }
    return {"items": [], "total": 0, "session_id": session_key}


@app.post("/api/chat/uploads")
async def upload(request: Request, filename: str = Query("upload.txt")) -> dict[str, str]:
    suffix = Path(filename).suffix or ".txt"
    bucket = UPLOADS / uuid4().hex
    bucket.mkdir(parents=True)
    safe_name = f"{Path(filename).stem or 'upload'}{suffix}"
    path = bucket / safe_name
    path.write_bytes(await request.body())
    return {
        "filename": safe_name,
        "upload_path": str(path),
        "upload_url": f"/api/chat/media?path={quote(str(path), safe='')}",
        "media_type": request.headers.get("content-type", "text/plain"),
    }


@app.get("/api/chat/media")
def media(path: str = Query(...)) -> FileResponse:
    return FileResponse(Path(path))


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    active_turn = ""
    try:
        while True:
            frame = await websocket.receive_json()
            frame_type = str(frame.get("type") or "")
            request_id = str(frame.get("request_id") or "")
            session_id = str(frame.get("session_id") or "web:playwright")
            if frame_type == "session.create":
                await websocket.send_json({"type": "session.created", "request_id": request_id, "session_id": "web:playwright"})
                continue
            if frame_type == "turn.stop":
                await websocket.send_json({"type": "turn.interrupted", "request_id": request_id, "session_id": session_id, "turn_id": active_turn, "status": "interrupted", "message": "已停止"})
                active_turn = ""
                continue
            if frame_type != "message.send":
                continue
            text = str(frame.get("text") or "")
            if text == "触发错误":
                await websocket.send_json({"type": "error", "request_id": request_id, "code": "fake_error", "message": "Fake 结构化错误"})
                continue
            active_turn = f"turn-{request_id}"
            await websocket.send_json({"type": "turn.started", "request_id": request_id, "session_id": session_id, "turn_id": active_turn})
            if text == "等待停止":
                continue
            if text == "长回答布局测试":
                paragraphs = "\n\n".join(
                    f"### 资料 {index}\n这是用于验证长回答滚动边界的内容。"
                    for index in range(1, 81)
                )
                content = (
                    f"{paragraphs}\n\n"
                    "https://example.com/this-is-an-intentionally-very-long-unbroken-reference-path-that-must-not-expand-the-layout\n\n"
                    "```python\nprint('layout')\n```\n\n"
                    "```mermaid\ngraph LR\n  A[WebSocket] --> B[Agent]\n```\n\n"
                    "长回答结束"
                )
                await websocket.send_json({"type": "message.final", "request_id": request_id, "session_id": session_id, "turn_id": active_turn, "content": content, "thinking": "", "media": []})
                active_turn = ""
                continue
            await websocket.send_json({"type": "react.thinking.delta", "session_id": session_id, "turn_id": active_turn, "delta": "正在分析用户请求"})
            await websocket.send_json({"type": "answer.delta", "session_id": session_id, "turn_id": active_turn, "delta": "流式草稿"})
            await websocket.send_json({"type": "react.tool.started", "session_id": session_id, "turn_id": active_turn, "call_id": "call-1", "tool_name": "list_dir", "arguments": {"path": "."}})
            await websocket.send_json({"type": "react.tool.completed", "session_id": session_id, "turn_id": active_turn, "call_id": "call-1", "tool_name": "list_dir", "status": "ok", "result_preview": "agent, tests"})
            content = "最终内容\n\n```python\ndef greet(name: str) -> str:\n    message = f'你好，{name}'\n    return message\n\nprint(greet('BeanAgent'))\n```\n\n```mermaid\ngraph LR\n  A[WebSocket] --> B[Agent]\n```"
            await websocket.send_json({"type": "message.final", "request_id": request_id, "session_id": session_id, "turn_id": active_turn, "content": content, "thinking": "已经分析用户请求", "media": []})
            active_turn = ""
            if text == "附件测试":
                # final 到达后主动断开，验证客户端保留完成消息并自动重连。
                await asyncio.sleep(0.05)
                await websocket.close()
                return
    except WebSocketDisconnect:
        return


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=4173, log_level="warning")
