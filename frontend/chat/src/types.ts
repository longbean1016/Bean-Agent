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
}

export interface ChatState {
  sessionId: string;
  activeTurnId: string;
  messages: ChatMessage[];
  error: string;
}

export interface SessionSummary {
  key: string;
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
}

export interface UploadedFile {
  filename: string;
  upload_path: string;
  upload_url: string;
  media_type: string;
}

export type ChatFrame =
  | { type: "session.created"; request_id: string; session_id: string }
  | { type: "turn.started"; request_id?: string; session_id: string; turn_id: string }
  | { type: "answer.delta"; session_id: string; turn_id: string; delta: string }
  | { type: "react.thinking.delta"; session_id: string; turn_id: string; delta: string }
  | { type: "react.tool.started"; session_id: string; turn_id: string; call_id: string; tool_name: string; arguments: unknown }
  | { type: "react.tool.completed"; session_id: string; turn_id: string; call_id: string; tool_name: string; status: string; result_preview: string }
  | { type: "message.final"; request_id?: string; session_id: string; turn_id: string; content: string; thinking?: string; media?: string[] }
  | { type: "turn.interrupted"; request_id: string; session_id: string; turn_id?: string; status: string; message?: string }
  | { type: "error"; request_id: string; code?: string; message: string }
  | { type: "pong"; request_id: string };

export type ChatAction =
  | ChatFrame
  | { type: "ui.session.select"; sessionId: string; messages: ChatMessage[] }
  | { type: "ui.user.append"; message: ChatMessage }
  | { type: "ui.error.clear" };
