export type ConnectionStatus = "connecting" | "connected" | "reconnecting" | "offline";
/** 工具状态必须保守归一化；未知状态不能被当成成功。 */
export type ToolStatus =
  | "running"
  | "completed"
  | "error"
  | "interrupted"
  | "cancelled"
  | "expired"
  | "unavailable"
  | "rejected"
  | "unknown";
export type ApprovalState = "none" | "pending" | "submitting" | "allowed-once" | "rejected" | "cancelled" | "expired" | "unavailable";
export type ThinkingStatus = "running" | "completed" | "interrupted";
export type SandboxMode = "read-only" | "workspace-write" | "danger-full-access";

export type ExtensionScope = "user" | "workspace";

export interface PluginExtensionRecord {
  id: string;
  name: string;
  description: string;
  version: string;
  source: string;
  scope: ExtensionScope;
  status: "installed" | "available" | "disabled" | "invalid";
  enabled: boolean;
  skills_count: number;
  mcp_count: number;
  commands_count: number;
  mcp_groups?: Array<{
    id: string;
    name: string;
    transport: string;
    status: "disabled" | "not_loaded" | "connecting" | "connected" | "error" | string;
    enabled: boolean;
  }>;
}

export interface McpExtensionRecord {
  id: string;
  name: string;
  description: string;
  scope: ExtensionScope;
  transport: "stdio" | "http" | "sse" | "unknown";
  status: "connected" | "connecting" | "disconnected" | "disabled" | "error" | "unknown" | "unsupported";
  enabled: boolean;
  tool_count: number;
  command?: string;
  url?: string;
  cwd?: string | null;
  env_names?: string[];
  header_names?: string[];
  tools?: string[];
  error?: string | null;
  timeout_ms?: number | null;
  revision: number;
  created_at?: string | null;
  updated_at?: string | null;
  oauth_configured?: boolean;
  authorization_required?: boolean;
}

export interface SkillExtensionRecord {
  id: string;
  name: string;
  description: string;
  source: "workspace" | "builtin" | "plugin" | string;
  scope: ExtensionScope;
  available: boolean;
  enabled: boolean;
  always: boolean;
  missing: string;
  plugin_name?: string | null;
  version?: string | null;
}

export interface Workspace {
  id: string;
  canonical_path: string;
  title: string;
  created_at: string;
  updated_at: string;
  pinned_at?: string | null;
  valid: boolean;
}

export interface SandboxSnapshot {
  session_id: string;
  workspace_id: string | null;
  cwd_snapshot?: string | null;
  workspace_title?: string | null;
  workspace_path?: string | null;
  workspace_valid: boolean;
  sandbox_mode: SandboxMode;
  backend: string;
  capability: "partial" | string;
}

export interface ApprovalRequest {
  id: string;
  session_id: string;
  turn_id: string;
  call_id: string;
  tool_name: string;
  operation: string;
  arguments: Record<string, unknown>;
  reason: string;
  requested_mode: SandboxMode;
  /** 默认 Web 投影不下发内部指纹；历史/调试回放若带有仍可兼容读取。 */
  fingerprint?: string;
  state: "pending";
  created_at: string;
  /** 后端可选的脱敏展示投影；前端不应自行解析原始 arguments。 */
  summary?: string;
  scope?: string;
  reason_code?: string;
  expires_at?: string;
  requested_at?: string;
}

/**
 * 已收到的审批终态短期缓存。它不是审计记录，只用于处理 resolved 先于
 * tool.started/approval.requested 到达时的乱序事件，并按 session 隔离。
 */
export interface ResolvedApproval {
  id: string;
  session_id: string;
  turn_id?: string;
  call_id?: string;
  decision?: "allowed-once" | "rejected" | "cancelled" | "expired" | "unavailable" | null;
  state: ApprovalState;
  requested_at?: string;
  decided_at?: string;
  error_code?: string;
}

export interface ToolActivity {
  callId: string;
  name: string;
  status: ToolStatus;
  arguments?: unknown;
  resultPreview: string;
  /** 工具生命周期时间，内部使用 camelCase，线上事件保持 snake_case。 */
  startedAt?: string;
  endedAt?: string;
  durationMs?: number;
  approvalRequestedAt?: string;
  approvalResolvedAt?: string;
  approvalWaitMs?: number;
  executionMs?: number;
  groupDurationMs?: number;
  approvalId?: string;
  approvalState?: ApprovalState;
  resultKind?: string;
  isTruncated?: boolean;
  exitCode?: number;
  errorCode?: string;
}

export interface ChatMessage {
  id: string;
  seq?: number;
  role: "user" | "assistant";
  content: string;
  thinking: string;
  media: string[];
  tools: ToolActivity[];
  turnId?: string;
  streaming?: boolean;
  thinkingStatus?: ThinkingStatus;
  status?: string;
  timestamp?: string;
  /** 本轮从用户发送到 assistant 完成/中断的前端展示耗时。 */
  durationMs?: number;
  proactive?: boolean;
  source?: "scheduled_reminder" | "scheduled_soft" | "proactive_conversation";
  scheduledAt?: string;
  modelRoute?: ModelRouteMetadata;
}

export interface ModelRouteMetadata {
  connection_id: string;
  model_id: string;
  connection_name?: string;
  model_display_name?: string;
  adapter?: ModelAdapterId;
}

export interface ChatState {
  sessionId: string;
  activeTurnId: string;
  messages: ChatMessage[];
  sessionMessages: Record<string, ChatMessage[]>;
  error: string;
  /** 按 session_id + approval id 保存尚未关联到工具行的请求，避免跨会话同名 id 覆盖。 */
  approvalRequests?: Record<string, Record<string, ApprovalRequest>>;
  /** 按 session_id + approval id 保存最近的审批终态，防止跨会话串联。 */
  resolvedApprovals?: Record<string, Record<string, ResolvedApproval>>;
  turnStates: Record<string, TurnRuntimeState>;
  contextUsage: Record<string, ContextUsage>;
  sessionUsage: Record<string, SessionUsage>;
}

export interface SessionUsage {
  totalUncachedInputTokens: number;
  totalCacheReadTokens: number;
  totalCacheWriteTokens: number;
  totalInputTokens: number;
  cacheHitRate: number | null;
  totalOutputTokens: number;
}

export interface ContextUsage {
  turnId: string;
  usedTokens: number;
  pressureTokens?: number;
  projectedTokens?: number;
  surfaceTokens?: number;
  systemTokens?: number;
  toolsTokens?: number;
  messageTokens?: number;
  asOfSeq?: number;
  modelRuntimeId?: string;
  model?: string;
  contextWindow: number;
  softLimitTokens: number;
  hardInputTokens: number;
  contextWindowSource: string;
  estimateSource: string;
  breakdown: {
    system_prompt_tokens: number;
    tools_tokens: number;
    conversation_tokens: number;
    overhead_tokens?: number;
  };
  sections: Array<{
    name: string;
    estimated_tokens: number;
    static: boolean;
    cache_hit: boolean;
  }>;
}

export interface TurnRuntimeState {
  status: "idle" | "submitting" | "queued" | "running" | "compacting";
  queuePosition: number | null;
  turnId: string;
  requestId: string;
}

export interface SessionSummary {
  key: string;
  title?: string;
  created_at: string;
  updated_at: string;
  last_activity_at?: string;
  pinned_at?: string | null;
  message_count: number;
  first_message_content: string;
  workspace_id?: string | null;
  cwd_snapshot?: string | null;
  workspace_title?: string | null;
  workspace_path?: string | null;
  workspace_valid?: boolean;
  sandbox_mode?: SandboxMode;
}

export interface MessageRow {
  id: string;
  seq?: number;
  role: string;
  content: string;
  turn_id?: string;
  reasoning_content?: string;
  interrupted_display_content?: string;
  interrupted_display_reasoning?: string;
  interrupted_thinking_status?: ThinkingStatus;
  media?: string[];
  tool_chain?: Array<{ calls?: Array<{
    call_id?: string;
    name?: string;
    arguments?: unknown;
    result?: string;
    result_preview?: string;
    status?: string;
    approval_id?: string | null;
    approval_state?: ApprovalState | string | null;
    started_at?: string | null;
    ended_at?: string | null;
    duration_ms?: number | string | null;
    approval_requested_at?: string | null;
    approval_resolved_at?: string | null;
    approval_wait_ms?: number | string | null;
    execution_ms?: number | string | null;
    group_duration_ms?: number | string | null;
    result_kind?: string | null;
    is_truncated?: boolean | null;
    exit_code?: number | string | null;
    error_code?: string | null;
  }> }>;
  status?: string;
  timestamp?: string;
  duration_ms?: number;
  elapsed_ms?: number;
  proactive?: boolean;
  metadata?: Record<string, unknown>;
}

export interface MessagePage {
  items: MessageRow[];
  total?: number;
  has_more?: boolean;
  next_before_seq?: number | null;
  has_before?: boolean;
  has_after?: boolean;
}

export interface TurnNavigationEntry {
  id: string;
  seq?: number;
  turnIndex?: number;
  question: string;
  preview: string;
  durationMs?: number;
  startedAt?: string;
  endedAt?: string;
}

export interface UploadedFile {
  filename: string;
  upload_path: string;
  upload_url: string;
  media_type: string;
}

export interface ProactiveSettings {
  session_key: string;
  reminders_enabled: boolean;
  reminder_quiet_policy: "delay" | "send" | "skip";
  conversation_enabled: boolean;
  activity_level: "restrained" | "balanced" | "active";
  min_conversation_interval_hours: number;
  daily_conversation_limit: number;
  quiet_hours_enabled: boolean;
  quiet_start: string;
  quiet_end: string;
  timezone: string;
}

export interface ScheduledReminder {
  id: string;
  name: string;
  tier: "instant" | "soft";
  trigger: "at" | "after" | "every";
  fire_at: string;
  enabled: boolean;
  status: string;
  run_count: number;
  last_error: string;
}

export interface ProactiveNotificationRow {
  id: string;
  content: string;
  source: "scheduled_reminder" | "scheduled_soft";
  source_id: string;
  scheduled_at: string;
  generated_at: string;
  delivered_at?: string | null;
  status: "pending" | "delivered" | "seen";
  recurring: boolean;
}

export type ModelAdapterId = "generic_openai" | "deepseek" | "qwen_dashscope" | "openai_reasoning";

/** 模型能力配置入口；primary 复用现有默认路由，另外两项可独立指定模型。 */
export type ModelCapability = "primary" | "embedding" | "vision";

/** 能力路由模式。后端可能返回 inherit/follow/follow_main，前端统一按 follow 处理。 */
export type CapabilityRouteMode = "follow" | "inherit" | "follow_main" | "independent";

export interface ModelProfile {
  connection_id: string;
  model_id: string;
  display_name: string;
  context_window: number | null;
  max_output_tokens: number | null;
  supports_tools: boolean | null;
  supports_vision: boolean | null;
  supports_reasoning: boolean | null;
  reasoning_options: string[];
  adapter: ModelAdapterId;
  metadata_source: string;
  metadata_updated_at: string | null;
  user_overrides: Record<string, unknown>;
  available: boolean;
  revision: number;
  discovered_at: string;
  protocol?: "chat_completions" | "responses" | string;
  capability_source?: "catalog" | "probe" | "manual" | "unknown" | string;
  capability_confidence?: "high" | "medium" | "low" | string;
  capabilities_json?: {
    reasoning?: {
      mode?: "toggle" | "effort" | string;
      native?: string[];
      aliases?: Record<string, string>;
      response_field?: string;
    };
    [key: string]: unknown;
  };
}

export interface ModelConnection {
  id: string;
  name: string;
  provider: string;
  base_url: string;
  has_api_key: boolean;
  api_key_preview?: string | null;
  enabled: boolean;
  default_adapter: ModelAdapterId;
  revision: number;
  created_at: string;
  updated_at: string;
  models: ModelProfile[];
}

export interface ModelRoute {
  connection_id: string;
  model_id: string;
  reasoning_effort?: string | null;
}

export interface CapabilityRouteState {
  capability?: ModelCapability | string;
  mode?: CapabilityRouteMode | string | null;
  follows_primary?: boolean;
  requires_restart?: boolean;
  runtime_effective_at?: "now" | "next_start" | string | null;
  route?: ModelRoute | null;
  override_route?: ModelRoute | null;
  connection?: ModelConnection | null;
  /** 测试结果仅用于设置页反馈，不持久化密钥。 */
  dimensions?: number | null;
  expected_dimension?: number | null;
}

export interface ModelSettingsPayload {
  connections: ModelConnection[];
  default_route: ModelRoute | null;
  catalog: { updated_at?: string | null };
  routing_required: boolean;
  /** 新版后端返回 capability_routes；capabilities 保留兼容预览和旧客户端。 */
  capability_routes?: Partial<Record<ModelCapability, CapabilityRouteState>>;
  capabilities?: Partial<Record<Exclude<ModelCapability, "primary">, CapabilityRouteState>>;
}

export type ChatFrame =
  | { type: "session.created"; request_id: string; session_id: string }
  | { type: "session.updated"; session: SessionSummary }
  | { type: "session.subscribed"; request_id: string; session_id: string }
  | { type: "sandbox.updated"; request_id: string; sandbox: SandboxSnapshot }
  | { type: "approval.requested"; session_id: string; approval: ApprovalRequest }
  | { type: "approval.resolved"; request_id: string; session_id: string; approval_id: string; decision?: "allowed-once" | "rejected" | "cancelled" | "expired" | "unavailable" | null; turn_id?: string; call_id?: string; state?: ApprovalState | string | null; decided_at?: string; error_code?: string }
  | { type: "turn.snapshot"; session_id: string; turn_id: string; request_id: string; user_message?: string; user_media?: string[]; content?: string; thinking?: string; tools?: Array<{ call_id: string; name: string; status: ToolStatus | string; arguments?: unknown; result_preview?: string; started_at?: string | null; ended_at?: string | null; duration_ms?: number | string | null; approval_id?: string | null; approval_state?: ApprovalState | string | null; approval_requested_at?: string | null; approval_resolved_at?: string | null; approval_wait_ms?: number | string | null; execution_ms?: number | string | null; group_duration_ms?: number | string | null; result_kind?: string | null; is_truncated?: boolean | null; exit_code?: number | string | null; error_code?: string | null }>; started_at?: string; status: "running" }
  | { type: "turn.queued"; request_id: string; session_id: string; position: number }
  | { type: "turn.started"; request_id?: string; session_id: string; turn_id: string }
  | { type: "context.compaction.started"; session_id: string; turn_id: string; trigger: string; estimated_tokens: number }
  | { type: "context.compaction.completed"; session_id: string; turn_id: string; trigger: string; estimated_tokens: number; compacted: boolean }
  | { type: "context.compaction.failed"; session_id: string; turn_id: string; trigger: string; estimated_tokens: number; message: string }
  | { type: "context.usage.reset"; session_id: string }
  | { type: "context.usage.updated"; session_id: string; turn_id: string; used_tokens: number; context_window: number; soft_limit_tokens: number; hard_input_tokens: number; context_window_source: string; estimate_source: string; breakdown: Record<string, number>; sections: Array<{ name: string; estimated_tokens: number; static: boolean; cache_hit: boolean }>; pressure_tokens?: number; projected_tokens?: number; surface_tokens?: number; system_tokens?: number; tools_tokens?: number; message_tokens?: number; as_of_seq?: number; model_runtime_id?: string; model?: string }
  | { type: "session.usage.updated"; session_id: string; turn_id: string; total_uncached_input_tokens: number; total_cache_read_tokens: number; total_cache_write_tokens: number; total_input_tokens: number; cache_hit_rate: number | null; total_output_tokens: number }
  | { type: "answer.delta"; session_id: string; turn_id: string; delta: string }
  | { type: "react.thinking.delta"; session_id: string; turn_id: string; delta: string }
  | { type: "react.tool.started"; session_id: string; turn_id: string; call_id: string; tool_name: string; arguments: unknown; started_at?: string; approval_id?: string; approval_state?: ApprovalState | string; approval_requested_at?: string | null }
  | { type: "react.tool.completed"; session_id: string; turn_id: string; call_id: string; tool_name: string; status: string; result_preview: string; started_at?: string; ended_at?: string; duration_ms?: number | string | null; approval_requested_at?: string; approval_resolved_at?: string; approval_wait_ms?: number | string | null; execution_ms?: number | string | null; group_duration_ms?: number | string | null; result_kind?: string; is_truncated?: boolean; exit_code?: number | string | null; error_code?: string }
  | { type: "message.final"; request_id?: string; session_id: string; turn_id: string; content: string; thinking?: string; media?: string[]; message_id?: string; metadata?: Record<string, unknown> }
  | { type: "turn.interrupted"; request_id: string; session_id: string; turn_id?: string; status: string; message?: string; duration_ms?: number; ended_at?: string }
  | { type: "error"; request_id: string; session_id?: string; code?: string; message: string }
  | { type: "pong"; request_id: string };

export type ChatAction =
  | ChatFrame
  | { type: "ui.session.select"; sessionId: string; messages: ChatMessage[]; replace?: boolean }
  | { type: "ui.user.append"; message: ChatMessage }
  | { type: "ui.turn.submitted"; sessionId: string; requestId: string }
  | { type: "ui.error.clear" };
