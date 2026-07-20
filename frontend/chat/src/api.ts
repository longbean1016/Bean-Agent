import type { MessageRow, ProactiveNotificationRow, ProactiveSettings, ScheduledReminder, SessionSummary, UploadedFile } from "./types";

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
