export type ConnectionStatus = "connecting" | "connected" | "reconnecting" | "offline";
export type ToolStatus = "running" | "completed" | "error";

export interface ToolActivity {
  callId: string;
  name: string;
  status: ToolStatus;
  arguments: unknown;
  resultPreview: string;
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  thinking: string;
  media: string[];
  tools: ToolActivity[];
  turnId?: string;
  streaming?: boolean;
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
  error: string;
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
  role: string;
  content: string;
  turn_id?: string;
  reasoning_content?: string;
  media?: string[];
  tool_chain?: Array<{ calls?: Array<{ call_id?: string; name?: string; arguments?: unknown; result?: string; status?: string }> }>;
  status?: string;
  timestamp?: string;
  proactive?: boolean;
  metadata?: Record<string, unknown>;
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
  | { type: "session.subscribed"; request_id: string; session_id: string }
  | { type: "turn.started"; request_id?: string; session_id: string; turn_id: string }
  | { type: "answer.delta"; session_id: string; turn_id: string; delta: string }
  | { type: "react.thinking.delta"; session_id: string; turn_id: string; delta: string }
  | { type: "react.tool.started"; session_id: string; turn_id: string; call_id: string; tool_name: string; arguments: unknown }
  | { type: "react.tool.completed"; session_id: string; turn_id: string; call_id: string; tool_name: string; status: string; result_preview: string }
  | { type: "message.final"; request_id?: string; session_id: string; turn_id: string; content: string; thinking?: string; media?: string[]; message_id?: string; metadata?: Record<string, unknown> }
  | { type: "turn.interrupted"; request_id: string; session_id: string; turn_id?: string; status: string; message?: string }
  | { type: "error"; request_id: string; code?: string; message: string }
  | { type: "pong"; request_id: string };

export type ChatAction =
  | ChatFrame
  | { type: "ui.session.select"; sessionId: string; messages: ChatMessage[] }
  | { type: "ui.user.append"; message: ChatMessage }
  | { type: "ui.error.clear" };
