import type { ChatAction, ChatMessage, ChatState, MessageRow, ToolActivity } from "./types";

export const initialChatState: ChatState = {
  sessionId: "",
  activeTurnId: "",
  messages: [],
  error: "",
};

export function reduceChatFrame(state: ChatState, action: ChatAction): ChatState {
  if (action.type === "ui.session.select") {
    return { ...initialChatState, sessionId: action.sessionId, messages: action.messages };
  }
  if (action.type === "ui.user.append") {
    return { ...state, messages: [...state.messages, action.message], error: "" };
  }
  if (action.type === "ui.error.clear") return { ...state, error: "" };
  if (action.type === "session.created") {
    return { ...initialChatState, sessionId: action.session_id };
  }
  if (action.type === "error") return { ...state, error: action.message };
  if (action.type === "pong") return state;
  if ("session_id" in action && state.sessionId && action.session_id !== state.sessionId) {
    return state;
  }
  if (action.type === "turn.started") {
    const draft: ChatMessage = {
      id: action.turn_id,
      turnId: action.turn_id,
      role: "assistant",
      content: "",
      thinking: "",
      media: [],
      tools: [],
      streaming: true,
    };
    return {
      ...state,
      sessionId: action.session_id,
      activeTurnId: action.turn_id,
      messages: [...state.messages.filter((item) => item.turnId !== action.turn_id), draft],
      error: "",
    };
  }
  if (action.type === "answer.delta") {
    return updateTurn(state, action.turn_id, (message) => ({
      ...message,
      content: message.content + action.delta,
      streaming: true,
    }));
  }
  if (action.type === "react.thinking.delta") {
    return updateTurn(state, action.turn_id, (message) => ({
      ...message,
      thinking: message.thinking + action.delta,
      streaming: true,
    }));
  }
  if (action.type === "react.tool.started") {
    return updateTurn(state, action.turn_id, (message) => ({
      ...message,
      tools: [...message.tools, {
        callId: action.call_id,
        name: action.tool_name,
        status: "running",
        arguments: action.arguments,
        resultPreview: "",
      }],
    }));
  }
  if (action.type === "react.tool.completed") {
    return updateTurn(state, action.turn_id, (message) => ({
      ...message,
      tools: message.tools.map((tool) => tool.callId === action.call_id ? {
        ...tool,
        status: action.status === "error" ? "error" : "completed",
        resultPreview: action.result_preview,
      } : tool),
    }));
  }
  if (action.type === "message.final") {
    // final 是服务端的权威快照，必须覆盖草稿，不能继续追加 delta。
    const next = updateTurn(state, action.turn_id, (message) => ({
      ...message,
      content: action.content || message.content,
      thinking: action.thinking || message.thinking,
      media: action.media ?? message.media,
      streaming: false,
    }));
    return { ...next, activeTurnId: "" };
  }
  if (action.type === "turn.interrupted") {
    return {
      ...state,
      activeTurnId: "",
      messages: state.messages.map((message) => message.turnId === action.turn_id || message.turnId === state.activeTurnId
        ? { ...message, streaming: false, status: "interrupted" }
        : message),
    };
  }
  return state;
}

function updateTurn(
  state: ChatState,
  turnId: string,
  updater: (message: ChatMessage) => ChatMessage,
): ChatState {
  const index = state.messages.findIndex((message) => message.turnId === turnId);
  if (index < 0) return state;
  const messages = [...state.messages];
  messages[index] = updater(messages[index]);
  return { ...state, messages };
}

export function rowsToMessages(rows: MessageRow[]): ChatMessage[] {
  return rows.filter((row) => row.role === "user" || row.role === "assistant").map((row) => ({
    id: row.id,
    role: row.role === "user" ? "user" : "assistant",
    content: row.content,
    thinking: row.reasoning_content ?? "",
    media: Array.isArray(row.media) ? row.media : [],
    tools: toolChainToActivities(row.tool_chain),
    turnId: row.turn_id,
    streaming: false,
    status: row.status,
    timestamp: row.timestamp,
  }));
}

function toolChainToActivities(chain: MessageRow["tool_chain"]): ToolActivity[] {
  return (chain ?? []).flatMap((group) => (group.calls ?? []).map((call) => ({
    callId: String(call.call_id ?? ""),
    name: String(call.name ?? "tool"),
    status: call.status === "error" ? "error" : "completed",
    arguments: call.arguments,
    resultPreview: String(call.result ?? ""),
  })));
}
