import type { MessageRow, SessionSummary, UploadedFile } from "./types";

export async function fetchSessions(): Promise<SessionSummary[]> {
  const response = await fetch("/api/chat/sessions?page=1&page_size=100");
  if (!response.ok) throw new Error("无法加载会话列表");
  const payload = await response.json() as { items?: SessionSummary[] };
  return payload.items ?? [];
}

export async function fetchMessages(sessionId: string): Promise<MessageRow[]> {
  const response = await fetch(`/api/chat/sessions/${encodeURIComponent(sessionId)}/messages?page=1&page_size=500`);
  if (!response.ok) throw new Error("无法加载会话历史");
  const payload = await response.json() as { items?: MessageRow[] };
  return payload.items ?? [];
}

export async function uploadAttachment(file: File): Promise<UploadedFile> {
  const response = await fetch(`/api/chat/uploads?filename=${encodeURIComponent(file.name)}`, {
    method: "POST",
    headers: { "content-type": file.type || "text/plain" },
    body: file,
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({})) as { detail?: string };
    throw new Error(payload.detail || `上传失败 (${response.status})`);
  }
  return response.json() as Promise<UploadedFile>;
}

export function mediaUrl(path: string): string {
  return `/api/chat/media?path=${encodeURIComponent(path)}`;
}
