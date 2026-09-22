import type { CapabilityRouteState, ExtensionScope, McpExtensionRecord, MessagePage, MessageRow, ModelCapability, ModelConnection, ModelProfile, ModelRoute, ModelSettingsPayload, PluginExtensionRecord, ProactiveNotificationRow, ProactiveSettings, ScheduledReminder, SessionSummary, SkillExtensionRecord, TurnNavigationEntry, UploadedFile, Workspace } from "./types";

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

export async function fetchPluginExtensions(scope: ExtensionScope): Promise<PluginExtensionRecord[]> {
  const response = await fetch(`/api/extensions/plugins?scope=${encodeURIComponent(scope)}`);
  if (!response.ok) throw new Error("无法加载插件列表");
  const payload = await response.json() as { items?: PluginExtensionRecord[] };
  return payload.items ?? [];
}

export async function fetchMcpExtensions(scope: ExtensionScope): Promise<McpExtensionRecord[]> {
  const response = await fetch(`/api/extensions/mcp?scope=${encodeURIComponent(scope)}`);
  if (!response.ok) throw new Error("无法加载 MCP 列表");
  const payload = await response.json() as { items?: McpExtensionRecord[] };
  return payload.items ?? [];
}

export interface ExternalMcpSource {
  id: string;
  agent: string;
  path: string;
  scope: "global" | "project";
  server_count: number;
  servers: Array<{ name: string; transport: string; summary: string; has_secrets: boolean }>;
}

export async function discoverMcpSources(): Promise<ExternalMcpSource[]> {
  const response = await fetch("/api/extensions/mcp/discover");
  if (!response.ok) throw new Error("无法发现外部 MCP 配置");
  const payload = await response.json() as { sources?: ExternalMcpSource[] };
  return payload.sources ?? [];
}

export interface McpExtensionConfigPayload {
  name?: string;
  revision?: number;
  scope?: ExtensionScope;
  type?: "stdio" | "http" | "sse";
  command?: string[] | string;
  args?: string[];
  env?: Record<string, string>;
  cwd?: string;
  url?: string;
  headers?: Record<string, string>;
  oauth?: Record<string, unknown>;
  timeoutMs?: number;
  enabled?: boolean;
}

export async function createMcpExtension(payload: McpExtensionConfigPayload): Promise<McpExtensionRecord> {
  const response = await fetch("/api/extensions/mcp", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
  });
  const result = await response.json().catch(() => ({})) as McpExtensionRecord & { detail?: string };
  if (!response.ok || !result.id) throw new Error(result.detail || "无法添加 MCP 服务");
  return result;
}

export async function updateMcpExtension(id: string, scope: ExtensionScope, payload: McpExtensionConfigPayload & { revision?: number }): Promise<McpExtensionRecord> {
  const response = await fetch(`/api/extensions/mcp/${encodeURIComponent(id)}`, {
    method: "PUT",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ ...payload, scope }),
  });
  const result = await response.json().catch(() => ({})) as McpExtensionRecord & { detail?: string };
  if (!response.ok || !result.id) throw new Error(result.detail || "无法更新 MCP 服务");
  return result;
}

export async function testMcpExtension(payload: McpExtensionConfigPayload): Promise<{ success: boolean; tool_count?: number; tools?: string[]; error?: string; code?: string }> {
  const name = payload.name || "test-mcp";
  const response = await fetch(`/api/extensions/mcp/${encodeURIComponent(name)}/test`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
  });
  const result = await response.json().catch(() => ({})) as { success?: boolean; detail?: string; tool_count?: number; tools?: string[]; error?: string; code?: string };
  if (!response.ok) throw new Error(result.detail || "MCP 连接测试失败");
  return { success: Boolean(result.success), tool_count: result.tool_count, tools: result.tools, error: result.error, code: result.code };
}

export async function setMcpEnabled(id: string, scope: ExtensionScope, enabled: boolean, revision?: number): Promise<McpExtensionRecord> {
  const revisionQuery = revision ? `&revision=${encodeURIComponent(String(revision))}` : "";
  const response = await fetch(`/api/extensions/mcp/${encodeURIComponent(id)}/${enabled ? "enable" : "disable"}?scope=${encodeURIComponent(scope)}${revisionQuery}`, { method: "POST" });
  const result = await response.json().catch(() => ({})) as McpExtensionRecord & { detail?: string };
  if (!response.ok || !result.id) throw new Error(result.detail || "无法更新 MCP 状态");
  return result;
}

export async function refreshMcpExtension(id: string, scope: ExtensionScope): Promise<McpExtensionRecord> {
  const response = await fetch(`/api/extensions/mcp/${encodeURIComponent(id)}/refresh?scope=${encodeURIComponent(scope)}`, { method: "POST" });
  const result = await response.json().catch(() => ({})) as { item?: McpExtensionRecord; detail?: string };
  if (!response.ok || !result.item) throw new Error(result.detail || "无法刷新 MCP 工具");
  return result.item;
}

export async function importMcpExtensions(config: unknown, scope: ExtensionScope): Promise<{ imported: Array<{ name: string; status: string }>; skipped: Array<{ name: string; status: string; reason?: string }>; failed: Array<{ name: string; status: string; reason?: string }> }> {
  const response = await fetch("/api/extensions/mcp/import", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ servers: config, scope }),
  });
  const result = await response.json().catch(() => ({})) as { imported?: Array<{ name: string; status: string }>; skipped?: Array<{ name: string; status: string; reason?: string }>; failed?: Array<{ name: string; status: string; reason?: string }>; detail?: string };
  if (!response.ok) throw new Error(result.detail || "MCP 导入失败");
  return { imported: result.imported ?? [], skipped: result.skipped ?? [], failed: result.failed ?? [] };
}

export async function importDiscoveredMcpExtensions(selections: Array<{ source_id: string; names: string[] }>, scope: ExtensionScope): Promise<{ imported: Array<{ name: string; status: string }>; skipped: Array<{ name: string; status: string; reason?: string }>; failed: Array<{ name: string; status: string; reason?: string }> }> {
  const response = await fetch("/api/extensions/mcp/import", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ selections, scope }),
  });
  const result = await response.json().catch(() => ({})) as { imported?: Array<{ name: string; status: string }>; skipped?: Array<{ name: string; status: string; reason?: string }>; failed?: Array<{ name: string; status: string; reason?: string }>; detail?: string };
  if (!response.ok) throw new Error(result.detail || "外部 MCP 导入失败");
  return { imported: result.imported ?? [], skipped: result.skipped ?? [], failed: result.failed ?? [] };
}

export async function removeMcpExtension(name: string, scope: ExtensionScope, revision?: number): Promise<void> {
  const revisionQuery = revision ? `&revision=${encodeURIComponent(String(revision))}` : "";
  const response = await fetch(`/api/extensions/mcp/${encodeURIComponent(name)}?scope=${encodeURIComponent(scope)}${revisionQuery}`, { method: "DELETE" });
  const result = await response.json().catch(() => ({})) as { detail?: string };
  if (!response.ok) throw new Error(result.detail || "无法移除 MCP 服务");
}

export async function fetchSkillExtensions(scope: ExtensionScope): Promise<SkillExtensionRecord[]> {
  const response = await fetch(`/api/extensions/skills?scope=${encodeURIComponent(scope)}`);
  if (!response.ok) throw new Error("无法加载技能列表");
  const payload = await response.json() as { items?: SkillExtensionRecord[] };
  return payload.items ?? [];
}

export async function fetchSkillDetail(id: string, scope: ExtensionScope): Promise<SkillExtensionRecord & { content?: string; diagnostics?: string[] }> {
  const name = id.includes(":") ? id.slice(id.indexOf(":") + 1) : id;
  const response = await fetch(`/api/extensions/skills/${encodeURIComponent(name)}?scope=${encodeURIComponent(scope)}`);
  const result = await response.json().catch(() => ({})) as SkillExtensionRecord & { detail?: string };
  if (!response.ok) throw new Error(result.detail || "无法加载 Skill 详情");
  return result;
}

export async function createSkillExtension(payload: { name: string; scope: ExtensionScope; content: string }): Promise<void> {
  const response = await fetch("/api/extensions/skills", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(payload) });
  const result = await response.json().catch(() => ({})) as { detail?: string };
  if (!response.ok) throw new Error(result.detail || "无法创建 Skill");
}

export async function updateSkillExtension(name: string, scope: ExtensionScope, content: string, revision?: number): Promise<void> {
  const response = await fetch(`/api/extensions/skills/${encodeURIComponent(name)}`, { method: "PUT", headers: { "content-type": "application/json" }, body: JSON.stringify({ scope, content, revision }) });
  const result = await response.json().catch(() => ({})) as { detail?: string };
  if (!response.ok) throw new Error(typeof result.detail === "string" ? result.detail : "无法更新 Skill");
}

export async function setSkillEnabled(name: string, scope: ExtensionScope, enabled: boolean): Promise<void> {
  const response = await fetch(`/api/extensions/skills/${encodeURIComponent(name)}/${enabled ? "enable" : "disable"}`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ scope }) });
  if (!response.ok) throw new Error("无法更新 Skill 状态");
}

export async function refreshSkillExtension(name: string, scope: ExtensionScope): Promise<void> {
  const response = await fetch(`/api/extensions/skills/${encodeURIComponent(name)}/refresh`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ scope }) });
  if (!response.ok) throw new Error("无法刷新 Skill");
}

export async function removeSkillExtension(name: string, scope: ExtensionScope): Promise<void> {
  const response = await fetch(`/api/extensions/skills/${encodeURIComponent(name)}?scope=${encodeURIComponent(scope)}`, { method: "DELETE" });
  if (!response.ok) throw new Error("无法删除 Skill");
}

export async function importSkillExtension(payload: { scope: ExtensionScope; type: "directory" | "git"; path?: string; url?: string; revision?: string; mode?: "copy" | "symlink" }): Promise<void> {
  const response = await fetch("/api/extensions/skills/import", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(payload) });
  const result = await response.json().catch(() => ({})) as { failed?: Array<{ reason?: string }> };
  if (!response.ok || result.failed?.length) throw new Error(result.failed?.[0]?.reason || "无法导入 Skill");
}

class SettingsRequestError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "SettingsRequestError";
    this.status = status;
  }
}

async function settingsRequest<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init);
  const payload = await response.json().catch(() => ({})) as T & { detail?: string };
  if (!response.ok) throw new SettingsRequestError(payload.detail || `模型设置请求失败 (${response.status})`, response.status);
  return payload;
}

export async function fetchModelSettings(): Promise<ModelSettingsPayload> {
  const payload = await settingsRequest<Partial<ModelSettingsPayload>>("/api/settings");
  return {
    connections: payload.connections ?? [],
    default_route: payload.default_route ?? null,
    catalog: payload.catalog ?? {},
    routing_required: payload.routing_required ?? false,
    capability_routes: payload.capability_routes ?? (payload.capabilities
      ? { ...payload.capabilities }
      : undefined),
    capabilities: payload.capabilities,
  };
}

/** 读取 Embedding/视觉能力的独立路由；缺少该接口时由调用方回退到主模型。 */
export async function fetchCapabilityRoute(capability: Exclude<ModelCapability, "primary">): Promise<CapabilityRouteState | null> {
  try {
    return await settingsRequest<CapabilityRouteState>(`/api/settings/capabilities/${encodeURIComponent(capability)}`);
  } catch (firstError) {
    if (!(firstError instanceof SettingsRequestError) || firstError.status !== 404) throw firstError;
    // 兼容先行实现的 routes/capability 路径；真正的错误仍交给页面展示。
    try {
      return await settingsRequest<CapabilityRouteState>(`/api/settings/routes/capability/${encodeURIComponent(capability)}`);
    } catch {
      throw firstError;
    }
  }
}

export async function saveCapabilityRoute(
  capability: Exclude<ModelCapability, "primary">,
  values: { mode: "follow" | "inherit" | "independent"; connection_id?: string; model_id?: string; reasoning_effort?: string | null },
): Promise<CapabilityRouteState> {
  try {
    return await settingsRequest<CapabilityRouteState>(`/api/settings/capabilities/${encodeURIComponent(capability)}`, {
      method: "PUT", headers: { "content-type": "application/json" }, body: JSON.stringify(values),
    });
  } catch (firstError) {
    if (!(firstError instanceof SettingsRequestError) || firstError.status !== 404) throw firstError;
    try {
      return await settingsRequest<CapabilityRouteState>(`/api/settings/routes/capability/${encodeURIComponent(capability)}`, {
        method: "PUT", headers: { "content-type": "application/json" }, body: JSON.stringify(values),
      });
    } catch {
      throw firstError;
    }
  }
}

export interface CapabilityTestResult {
  ok: boolean;
  capability?: ModelCapability | string;
  connection_id?: string;
  connection_name?: string;
  model_id?: string;
  model_display_name?: string;
  dimensions?: number | null;
  expected_dimension?: number | null;
  vision_received?: boolean;
  duration_ms?: number;
  error_code?: string;
  detail?: string;
}

export async function testCapability(
  capability: Exclude<ModelCapability, "primary">,
  values: { connection_id?: string; model_id?: string; dimensions?: number | null },
): Promise<CapabilityTestResult> {
  return settingsRequest<CapabilityTestResult>(`/api/settings/capabilities/${encodeURIComponent(capability)}/test`, {
    method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(values),
  });
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

export async function testModelConnection(id: string): Promise<{ ok: boolean; connection_id: string; connection_name: string; model_count: number }> {
  return settingsRequest(`/api/settings/connections/${encodeURIComponent(id)}/test`, { method: "POST" });
}

export async function refreshConnectionModels(id: string): Promise<{ items: ModelProfile[]; catalog_warning?: string | null }> {
  return settingsRequest(`/api/settings/connections/${encodeURIComponent(id)}/models/refresh`, { method: "POST" });
}

export async function testConnectionModel(connectionId: string, modelId: string, reasoning_effort?: string | null, probe_type?: "basic" | "reasoning" | "tool_call"): Promise<{
  ok: boolean;
  connection_id: string;
  connection_name: string;
  model_id: string;
  model_display_name: string;
  adapter: string;
  requested_effort?: string | null;
  effective_effort?: string | null;
  thinking_received?: boolean;
  probe_type?: "basic" | "reasoning" | "tool_call";
  tool_call_received?: boolean;
  duration_ms: number;
}> {
  return settingsRequest(`/api/settings/connections/${encodeURIComponent(connectionId)}/models/${encodeURIComponent(modelId)}/test`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ reasoning_effort: reasoning_effort || null, probe_type: probe_type || null }),
  });
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

export async function updateModelCatalog(): Promise<{ updated_at: string; providers: number; models: number }> {
  return settingsRequest("/api/settings/catalog/update", { method: "POST" });
}
