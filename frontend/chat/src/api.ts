import type { MessagePage, MessageRow, ProactiveNotificationRow, ProactiveSettings, ScheduledReminder, SessionSummary, TurnNavigationEntry, UploadedFile } from "./types";

// 仅控制聊天页面的滚动分页，不参与模型上下文 token gate 或 checkpoint 边界。
const MESSAGE_WINDOW_LIMIT = 60;

export async function fetchSessions(): Promise<SessionSummary[]> {
  const response = await fetch("/api/chat/sessions?page=1&page_size=100");
  if (!response.ok) throw new Error("无法加载会话列表");
  const payload = await response.json() as { items?: SessionSummary[] };
  return payload.items ?? [];
}

export async function fetchMessages(sessionId: string): Promise<MessageRow[]> {
  const payload = await fetchMessagePage(sessionId);
  return payload.items ?? [];
}

export async function fetchMessagePage(sessionId: string): Promise<MessagePage> {
  const response = await fetch(`/api/chat/sessions/${encodeURIComponent(sessionId)}/messages`);
  if (!response.ok) throw new Error("无法加载会话历史");
  return response.json() as Promise<MessagePage>;
}

export async function fetchOlderMessages(sessionId: string, beforeSeq: number, limit = MESSAGE_WINDOW_LIMIT): Promise<MessagePage> {
  const response = await fetch(`/api/chat/sessions/${encodeURIComponent(sessionId)}/messages/older?before_seq=${encodeURIComponent(String(beforeSeq))}&limit=${encodeURIComponent(String(limit))}`);
  if (!response.ok) throw new Error("无法加载更早会话历史");
  return response.json() as Promise<MessagePage>;
}

export async function fetchMessagesAround(sessionId: string, anchorSeq: number, limit = MESSAGE_WINDOW_LIMIT): Promise<MessageRow[]> {
  const payload = await fetchMessagesAroundPage(sessionId, anchorSeq, limit);
  return payload.items ?? [];
}

export async function fetchMessagesAroundPage(sessionId: string, anchorSeq: number, limit = MESSAGE_WINDOW_LIMIT): Promise<MessagePage> {
  const response = await fetch(`/api/chat/sessions/${encodeURIComponent(sessionId)}/messages/around?anchor_seq=${encodeURIComponent(String(anchorSeq))}&limit=${encodeURIComponent(String(limit))}`);
  if (!response.ok) throw new Error("无法定位会话轮次");
  return response.json() as Promise<MessagePage>;
}

export async function fetchTurns(sessionId: string): Promise<TurnNavigationEntry[]> {
  const response = await fetch(`/api/chat/sessions/${encodeURIComponent(sessionId)}/turns`);
  if (!response.ok) throw new Error("无法加载会话导航");
  const payload = await response.json() as { items?: Array<{ id?: string; seq?: number; turn_index?: number; question?: string; preview?: string; duration_ms?: number; started_at?: string; ended_at?: string }> };
  return (payload.items ?? []).filter((item) => typeof item.id === "string").map((item) => ({
    id: String(item.id),
    seq: typeof item.seq === "number" ? item.seq : undefined,
    turnIndex: typeof item.turn_index === "number" ? item.turn_index : undefined,
    question: String(item.question ?? item.preview ?? ""),
    preview: String(item.preview ?? item.question ?? ""),
    durationMs: typeof item.duration_ms === "number" ? item.duration_ms : undefined,
    startedAt: typeof item.started_at === "string" ? item.started_at : undefined,
    endedAt: typeof item.ended_at === "string" ? item.ended_at : undefined,
  }));
}

export async function fetchNotifications(sessionId: string): Promise<ProactiveNotificationRow[]> {
  const response = await fetch(`/api/chat/sessions/${encodeURIComponent(sessionId)}/notifications`);
  if (!response.ok) throw new Error("无法加载提醒通知");
  return ((await response.json()) as { items?: ProactiveNotificationRow[] }).items ?? [];
}

export async function renameSession(sessionId: string, title: string): Promise<SessionSummary> {
  const response = await fetch(`/api/chat/sessions/${encodeURIComponent(sessionId)}`, {
    method: "PATCH",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ title }),
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({})) as { detail?: string };
    throw new Error(payload.detail || "无法重命名会话");
  }
  return response.json() as Promise<SessionSummary>;
}

export async function deleteSession(sessionId: string): Promise<void> {
  const response = await fetch(`/api/chat/sessions/${encodeURIComponent(sessionId)}`, {
    method: "DELETE",
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({})) as { detail?: string };
    throw new Error(payload.detail || "无法删除会话");
  }
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

export async function fetchProactiveSettings(sessionId: string): Promise<ProactiveSettings> {
  const response = await fetch(`/api/chat/sessions/${encodeURIComponent(sessionId)}/proactive`);
  if (!response.ok) throw new Error("无法加载主动设置");
  return ((await response.json()) as { settings: ProactiveSettings }).settings;
}

export async function saveProactiveSettings(sessionId: string, settings: ProactiveSettings): Promise<ProactiveSettings> {
  const response = await fetch(`/api/chat/sessions/${encodeURIComponent(sessionId)}/proactive`, {
    method: "PUT",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(settings),
  });
  const payload = await response.json().catch(() => ({})) as { settings?: ProactiveSettings; detail?: string };
  if (!response.ok || !payload.settings) throw new Error(payload.detail || "无法保存主动设置");
  return payload.settings;
}

export async function fetchReminders(sessionId: string): Promise<ScheduledReminder[]> {
  const response = await fetch(`/api/chat/sessions/${encodeURIComponent(sessionId)}/reminders`);
  if (!response.ok) throw new Error("无法加载提醒列表");
  return ((await response.json()) as { items?: ScheduledReminder[] }).items ?? [];
}

export async function deleteReminder(sessionId: string, reminderId: string): Promise<void> {
  const response = await fetch(`/api/chat/sessions/${encodeURIComponent(sessionId)}/reminders/${encodeURIComponent(reminderId)}`, { method: "DELETE" });
  if (!response.ok) throw new Error("无法删除提醒");
}
