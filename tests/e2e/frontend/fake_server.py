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

DEMO_WORKSPACE = {
    "id": "workspace-demo",
    "canonical_path": "D:/projects/bean-demo",
    "title": "Bean Demo",
    "created_at": "2026-09-03T08:00:00+08:00",
    "updated_at": "2026-09-03T08:00:00+08:00",
    "pinned_at": None,
    "valid": True,
}


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
            "last_activity_at": "2026-07-16T09:00:00+08:00",
            "pinned_at": None,
            "message_count": 2,
            "first_message_content": "历史问题",
            "workspace_id": DEMO_WORKSPACE["id"],
            "workspace_title": DEMO_WORKSPACE["title"],
            "workspace_path": DEMO_WORKSPACE["canonical_path"],
            "workspace_valid": True,
            "sandbox_mode": "workspace-write",
        }],
        "total": 1,
    }


@app.get("/api/chat/workspaces")
def workspaces() -> dict[str, object]:
    return {"items": [DEMO_WORKSPACE]}


@app.post("/api/chat/workspaces", status_code=201)
async def register_workspace(request: Request) -> dict[str, object]:
    payload = await request.json()
    path = str(payload.get("path") or "").strip()
    title = str(payload.get("title") or "").strip()
    return {
        **DEMO_WORKSPACE,
        "id": "workspace-added",
        "canonical_path": path,
        "title": title or Path(path).name,
    }


@app.post("/api/chat/workspaces/pick")
def pick_workspace() -> dict[str, str]:
    return {"path": "D:/projects/picked-by-native-dialog"}


@app.patch("/api/chat/workspaces/{workspace_id}")
async def update_workspace(workspace_id: str, request: Request) -> dict[str, object]:
    payload = await request.json()
    return {
        **DEMO_WORKSPACE,
        "id": workspace_id,
        "title": str(payload.get("title") or DEMO_WORKSPACE["title"]),
        "pinned_at": (
            "2026-09-03T12:00:00+08:00" if payload.get("pinned") else None
        ),
    }


@app.post("/api/chat/workspaces/{workspace_id}/open", status_code=204)
def open_workspace(workspace_id: str) -> None:
    return None


@app.delete("/api/chat/workspaces/{workspace_id}", status_code=204)
def delete_workspace(workspace_id: str) -> None:
    return None


@app.patch("/api/chat/sessions/{session_key:path}")
async def update_session(session_key: str, request: Request) -> dict[str, object]:
    payload = await request.json()
    return {
        "key": session_key,
        "title": str(payload.get("title") or ""),
        "updated_at": "2026-09-03T12:00:00+08:00",
        "last_activity_at": "2026-07-16T09:00:00+08:00",
        "pinned_at": (
            "2026-09-03T12:00:00+08:00" if payload.get("pinned") else None
        ),
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


@app.get("/api/chat/sessions/{session_key:path}/notifications")
def notifications(session_key: str) -> dict[str, object]:
    return {"items": [], "total": 0, "session_id": session_key}


@app.get("/api/chat/sessions/{session_key:path}/turns")
def turns(session_key: str) -> dict[str, object]:
    return {"items": [], "session_id": session_key}


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
    queued_request = ""
    current_session_id = "web:playwright"
    workspace_id: str | None = None
    sandbox_mode = "read-only"
    pending_approval = ""

    def sandbox_snapshot() -> dict[str, object]:
        has_workspace = workspace_id == DEMO_WORKSPACE["id"]
        return {
            "session_id": current_session_id,
            "workspace_id": workspace_id,
            "cwd_snapshot": DEMO_WORKSPACE["canonical_path"] if has_workspace else None,
            "workspace_title": DEMO_WORKSPACE["title"] if has_workspace else None,
            "workspace_path": DEMO_WORKSPACE["canonical_path"] if has_workspace else None,
            "workspace_valid": has_workspace,
            "sandbox_mode": sandbox_mode,
            "backend": "windows-acl",
            "capability": "partial",
        }

    def approval_snapshot() -> dict[str, object]:
        return {
            "id": pending_approval,
            "session_id": current_session_id,
            "turn_id": active_turn,
            "call_id": "call-approval",
            "tool_name": "shell",
            "operation": "执行完整 Shell 命令",
            "arguments": {
                "command": "Set-Content D:\\outside.txt 'approved'",
                "cwd": "D:/projects/bean-demo",
            },
            "reason": "命令需要写入当前工作目录之外的位置",
            "requested_mode": "danger-full-access",
            "fingerprint": "playwright-fingerprint",
            "state": "pending",
            "created_at": "2026-09-03T12:00:00+08:00",
        }

    try:
        while True:
            frame = await websocket.receive_json()
            frame_type = str(frame.get("type") or "")
            request_id = str(frame.get("request_id") or "")
            session_id = str(frame.get("session_id") or current_session_id)
            if frame_type == "session.create":
                current_session_id = "web:playwright"
                workspace_id = str(frame.get("workspace_id") or "") or None
                sandbox_mode = str(frame.get("sandbox_mode") or "read-only")
                await websocket.send_json({"type": "session.created", "request_id": request_id, "session_id": current_session_id})
                await websocket.send_json({"type": "sandbox.updated", "request_id": request_id, "sandbox": sandbox_snapshot()})
                continue
            if frame_type == "session.subscribe":
                current_session_id = session_id
                if session_id == "web:history":
                    workspace_id = str(DEMO_WORKSPACE["id"])
                    sandbox_mode = "workspace-write"
                await websocket.send_json({"type": "session.subscribed", "request_id": request_id, "session_id": session_id})
                await websocket.send_json({"type": "sandbox.updated", "request_id": "", "sandbox": sandbox_snapshot()})
                if pending_approval:
                    await websocket.send_json({"type": "approval.requested", "session_id": session_id, "approval": approval_snapshot()})
                continue
            if frame_type == "sandbox.mode.set":
                sandbox_mode = str(frame.get("sandbox_mode") or "read-only")
                await websocket.send_json({"type": "sandbox.updated", "request_id": request_id, "sandbox": sandbox_snapshot()})
                continue
            if frame_type == "workspace.bind":
                workspace_id = str(frame.get("workspace_id") or "") or None
                if workspace_id is None and sandbox_mode == "workspace-write":
                    sandbox_mode = "read-only"
                await websocket.send_json({"type": "sandbox.updated", "request_id": request_id, "sandbox": sandbox_snapshot()})
                continue
            if frame_type == "approval.decide":
                approval_id = str(frame.get("approval_id") or "")
                decision = str(frame.get("decision") or "rejected")
                await websocket.send_json({"type": "approval.resolved", "request_id": request_id, "session_id": session_id, "approval_id": approval_id, "decision": decision})
                if approval_id == pending_approval:
                    await websocket.send_json({"type": "message.final", "request_id": request_id, "session_id": session_id, "turn_id": active_turn, "content": "审批流程已结束", "thinking": "", "media": []})
                    pending_approval = ""
                    active_turn = ""
                continue
            if frame_type == "turn.stop":
                status = "cancelled" if queued_request else "interrupted"
                await websocket.send_json({"type": "turn.interrupted", "request_id": request_id, "session_id": session_id, "turn_id": active_turn, "status": status, "message": "已停止"})
                active_turn = ""
                queued_request = ""
                continue
            if frame_type != "message.send":
                continue
            if not frame.get("session_id"):
                current_session_id = "web:playwright"
                session_id = current_session_id
                workspace_id = str(frame.get("workspace_id") or "") or None
                sandbox_mode = str(frame.get("sandbox_mode") or "read-only")
                await websocket.send_json({"type": "session.created", "request_id": request_id, "session_id": session_id})
                await websocket.send_json({"type": "sandbox.updated", "request_id": request_id, "sandbox": sandbox_snapshot()})
            text = str(frame.get("text") or "")
            if text == "触发错误":
                await websocket.send_json({"type": "error", "request_id": request_id, "code": "fake_error", "message": "Fake 结构化错误"})
                continue
            if text == "排队测试":
                queued_request = request_id
                await websocket.send_json({"type": "turn.queued", "request_id": request_id, "session_id": session_id, "position": 1})
                await asyncio.sleep(0.05)
                await websocket.send_json({"type": "turn.queued", "request_id": request_id, "session_id": session_id, "position": 2})
                continue
            active_turn = f"turn-{request_id}"
            await websocket.send_json({"type": "turn.started", "request_id": request_id, "session_id": session_id, "turn_id": active_turn})
            if text == "审批测试":
                pending_approval = "approval-playwright"
                await websocket.send_json({
                    "type": "approval.requested",
                    "session_id": session_id,
                    "approval": approval_snapshot(),
                })
                continue
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
