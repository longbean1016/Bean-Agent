export type ConnectionStatus = "connecting" | "connected" | "reconnecting" | "offline";
export type ToolStatus = "running" | "completed" | "error" | "interrupted";
export type ThinkingStatus = "running" | "completed" | "interrupted";

export interface ToolActivity {
  callId: string;
  name: string;
  status: ToolStatus;
  arguments: unknown;
  resultPreview: string;
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
  proactive?: boolean;
  source?: "scheduled_reminder" | "scheduled_soft" | "proactive_conversation";
  scheduledAt?: string;
}

export interface ChatState {
  sessionId: string;
  activeTurnId: string;
  messages: ChatMessage[];
  sessionMessages: Record<string, ChatMessage[]>;
  error: string;
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
  message_count: number;
  first_message_content: string;
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
  tool_chain?: Array<{ calls?: Array<{ call_id?: string; name?: string; arguments?: unknown; result?: string; status?: string }> }>;
  status?: string;
  timestamp?: string;
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

export type ChatFrame =
  | { type: "session.created"; request_id: string; session_id: string }
  | { type: "session.updated"; session: SessionSummary }
  | { type: "session.subscribed"; request_id: string; session_id: string }
  | { type: "turn.snapshot"; session_id: string; turn_id: string; request_id: string; user_message: string; user_media: string[]; content: string; thinking: string; tools: Array<{ call_id: string; name: string; status: ToolStatus | string; arguments: unknown; result_preview: string }>; status: "running" }
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
  | { type: "react.tool.started"; session_id: string; turn_id: string; call_id: string; tool_name: string; arguments: unknown }
  | { type: "react.tool.completed"; session_id: string; turn_id: string; call_id: string; tool_name: string; status: string; result_preview: string }
  | { type: "message.final"; request_id?: string; session_id: string; turn_id: string; content: string; thinking?: string; media?: string[]; message_id?: string; metadata?: Record<string, unknown> }
  | { type: "turn.interrupted"; request_id: string; session_id: string; turn_id?: string; status: string; message?: string }
  | { type: "error"; request_id: string; session_id?: string; code?: string; message: string }
  | { type: "pong"; request_id: string };

export type ChatAction =
  | ChatFrame
  | { type: "ui.session.select"; sessionId: string; messages: ChatMessage[]; replace?: boolean }
  | { type: "ui.user.append"; message: ChatMessage }
  | { type: "ui.turn.submitted"; sessionId: string; requestId: string }
  | { type: "ui.error.clear" };
