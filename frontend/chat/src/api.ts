import type { MessagePage, MessageRow, ModelConnection, ModelProfile, ModelRoute, ModelSettingsPayload, ProactiveNotificationRow, ProactiveSettings, ScheduledReminder, SessionSummary, TurnNavigationEntry, UploadedFile, Workspace } from "./types";

// 仅控制聊天页面的滚动分页，不参与模型上下文 token gate 或 checkpoint 边界。
const MESSAGE_WINDOW_LIMIT = 60;

export async function fetchSessions(): Promise<SessionSummary[]> {
  const response = await fetch("/api/chat/sessions?page=1&page_size=100");
  if (!response.ok) throw new Error("无法加载会话列表");
  const payload = await response.json() as { items?: SessionSummary[] };
  return payload.items ?? [];
}

export async function fetchWorkspaces(): Promise<Workspace[]> {
  const response = await fetch("/api/chat/workspaces");
  if (!response.ok) throw new Error("无法加载工作目录");
  const payload = await response.json() as { items?: Workspace[] };
  return (payload.items ?? []).filter((item) => (
    typeof item?.id === "string" && typeof item.canonical_path === "string"
  ));
}

export async function registerWorkspace(path: string, title: string): Promise<Workspace> {
  const response = await fetch("/api/chat/workspaces", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ path, title }),
  });
  const payload = await response.json().catch(() => ({})) as Partial<Workspace> & { detail?: string };
  if (!response.ok || !payload.id) throw new Error(payload.detail || "无法添加工作目录");
  return payload as Workspace;
}

export async function pickWorkspaceDirectory(): Promise<string | null> {
  const response = await fetch("/api/chat/workspaces/pick", { method: "POST" });
  const payload = await response.json().catch(() => ({})) as { path?: string | null; detail?: string };
  if (!response.ok) throw new Error(payload.detail || "无法打开系统文件夹选择器");
  return typeof payload.path === "string" ? payload.path : null;
}

export async function updateWorkspace(
  workspaceId: string,
  patch: { title?: string; pinned?: boolean },
): Promise<Workspace> {
  const response = await fetch(`/api/chat/workspaces/${encodeURIComponent(workspaceId)}`, {
    method: "PATCH",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(patch),
  });
  const payload = await response.json().catch(() => ({})) as Partial<Workspace> & { detail?: string };
  if (!response.ok || !payload.id) throw new Error(payload.detail || "无法更新工作目录");
  return payload as Workspace;
}

export async function openWorkspaceDirectory(workspaceId: string): Promise<void> {
  const response = await fetch(`/api/chat/workspaces/${encodeURIComponent(workspaceId)}/open`, {
    method: "POST",
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({})) as { detail?: string };
    throw new Error(payload.detail || "无法在资源管理器中打开工作目录");
  }
}

export async function deleteWorkspace(workspaceId: string): Promise<void> {
  const response = await fetch(`/api/chat/workspaces/${encodeURIComponent(workspaceId)}`, {
    method: "DELETE",
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({})) as { detail?: string };
    throw new Error(payload.detail || "无法移除工作目录");
  }
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

export async function setSessionPinned(sessionId: string, pinned: boolean): Promise<void> {
  const response = await fetch(`/api/chat/sessions/${encodeURIComponent(sessionId)}`, {
    method: "PATCH",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ pinned }),
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({})) as { detail?: string };
    throw new Error(payload.detail || "无法更新会话置顶状态");
  }
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

async function settingsRequest<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init);
  const payload = await response.json().catch(() => ({})) as T & { detail?: string };
  if (!response.ok) throw new Error(payload.detail || `模型设置请求失败 (${response.status})`);
  return payload;
}

export async function fetchModelSettings(): Promise<ModelSettingsPayload> {
  const payload = await settingsRequest<Partial<ModelSettingsPayload>>("/api/settings");
  return {
    connections: payload.connections ?? [],
    default_route: payload.default_route ?? null,
    catalog: payload.catalog ?? {},
    routing_required: payload.routing_required ?? false,
  };
}

export async function fetchSessionModelRoute(sessionId: string): Promise<ModelRoute | null> {
  const payload = await settingsRequest<{ route: ModelRoute | null }>(`/api/settings/routes/session/${encodeURIComponent(sessionId)}`);
  return payload.route;
}

export async function saveSessionModelRoute(sessionId: string, route: ModelRoute): Promise<ModelRoute> {
  const payload = await settingsRequest<{ route: ModelRoute }>(`/api/settings/routes/session/${encodeURIComponent(sessionId)}`, {
    method: "PUT", headers: { "content-type": "application/json" }, body: JSON.stringify(route),
  });
  return payload.route;
}

export async function createModelConnection(values: Record<string, unknown>): Promise<ModelConnection> {
  return settingsRequest("/api/settings/connections", {
    method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(values),
  });
}

export async function updateModelConnection(id: string, values: Record<string, unknown>): Promise<ModelConnection> {
  return settingsRequest(`/api/settings/connections/${encodeURIComponent(id)}`, {
    method: "PUT", headers: { "content-type": "application/json" }, body: JSON.stringify(values),
  });
}

export async function deleteModelConnection(id: string): Promise<void> {
  const response = await fetch(`/api/settings/connections/${encodeURIComponent(id)}`, { method: "DELETE" });
  if (!response.ok) throw new Error("无法删除连接");
}

export async function testModelConnection(id: string): Promise<{ ok: boolean; model_count: number }> {
  return settingsRequest(`/api/settings/connections/${encodeURIComponent(id)}/test`, { method: "POST" });
}

export async function refreshConnectionModels(id: string): Promise<ModelProfile[]> {
  const payload = await settingsRequest<{ items: ModelProfile[] }>(`/api/settings/connections/${encodeURIComponent(id)}/models/refresh`, { method: "POST" });
  return payload.items;
}

export async function createManualModel(id: string, values: Record<string, unknown>): Promise<ModelProfile> {
  return settingsRequest(`/api/settings/connections/${encodeURIComponent(id)}/models`, {
    method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(values),
  });
}

export async function updateModelProfile(connectionId: string, modelId: string, values: Record<string, unknown>): Promise<ModelProfile> {
  return settingsRequest(`/api/settings/connections/${encodeURIComponent(connectionId)}/models/${encodeURIComponent(modelId)}`, {
    method: "PATCH", headers: { "content-type": "application/json" }, body: JSON.stringify(values),
  });
}

export async function saveDefaultModelRoute(route: ModelRoute): Promise<ModelRoute> {
  const payload = await settingsRequest<{ route: ModelRoute }>("/api/settings/routes/default", {
    method: "PUT", headers: { "content-type": "application/json" }, body: JSON.stringify(route),
  });
  return payload.route;
}
