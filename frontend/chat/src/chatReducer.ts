import type { ChatAction, ChatMessage, ChatState, MessageRow, ProactiveNotificationRow, ToolActivity, TurnRuntimeState } from "./types";

export const idleTurnState: TurnRuntimeState = {
  status: "idle",
  queuePosition: null,
  turnId: "",
  requestId: "",
};

export const initialChatState: ChatState = {
  sessionId: "",
  activeTurnId: "",
  messages: [],
  error: "",
  turnStates: {},
};

export function reduceChatFrame(state: ChatState, action: ChatAction): ChatState {
  if (action.type === "ui.session.select") {
    const turn = state.turnStates[action.sessionId] ?? idleTurnState;
    const messages = turn.status === "running" && turn.turnId
      && !action.messages.some((message) => message.turnId === turn.turnId)
      ? [...action.messages, createDraft(turn.turnId)]
      : action.messages;
    return {
      ...state,
      sessionId: action.sessionId,
      activeTurnId: turn.status === "running" ? turn.turnId : "",
      messages,
      error: "",
    };
  }
  if (action.type === "ui.user.append") {
    return { ...state, messages: [...state.messages, action.message], error: "" };
  }
  if (action.type === "ui.error.clear") return { ...state, error: "" };
  if (action.type === "session.created") {
    return { ...state, sessionId: action.session_id, activeTurnId: "", messages: [], error: "" };
  }
  if (action.type === "session.subscribed") return state;
  if (action.type === "error") {
    const rejected = action.code === "queue_full" || action.code === "session_busy" || action.code === "closed";
    if (!rejected || !action.session_id) return { ...state, error: action.message };
    const current = action.session_id === state.sessionId;
    return {
      ...state,
      activeTurnId: current ? "" : state.activeTurnId,
      messages: current
        ? state.messages.filter((message) => message.id !== `user-${action.request_id}`)
        : state.messages,
      error: current ? action.message : state.error,
      turnStates: setTurnState(state, action.session_id, idleTurnState),
    };
  }
  if (action.type === "pong") return state;
  if (action.type === "turn.queued") {
    return {
      ...state,
      error: action.session_id === state.sessionId ? "" : state.error,
      turnStates: setTurnState(state, action.session_id, {
        status: "queued",
        queuePosition: action.position,
        turnId: "",
        requestId: action.request_id,
      }),
    };
  }
  if (action.type === "turn.started") {
    const turnStates = setTurnState(state, action.session_id, {
      status: "running",
      queuePosition: null,
      turnId: action.turn_id,
      requestId: action.request_id ?? "",
    });
    if (state.sessionId && action.session_id !== state.sessionId) {
      return { ...state, turnStates };
    }
    const draft = createDraft(action.turn_id);
    return {
      ...state,
      sessionId: action.session_id,
      activeTurnId: action.turn_id,
      turnStates,
      messages: [...state.messages.filter((item) => item.turnId !== action.turn_id), draft],
      error: "",
    };
  }
  if (action.type === "message.final" && action.turn_id) {
    const nextState = {
      ...state,
      turnStates: setTurnState(state, action.session_id, idleTurnState),
    };
    if (state.sessionId && action.session_id !== state.sessionId) return nextState;
    state = nextState;
  }
  if (action.type === "turn.interrupted") {
    const current = action.session_id === state.sessionId;
    const interruptedTurnId = action.turn_id || state.activeTurnId;
    const nextState = {
      ...state,
      activeTurnId: current ? "" : state.activeTurnId,
      turnStates: setTurnState(state, action.session_id, idleTurnState),
    };
    if (!current) return nextState;
    return {
      ...nextState,
      messages: interruptedTurnId
        ? state.messages.map((message) => message.turnId === interruptedTurnId
          ? { ...message, streaming: false, status: "interrupted" }
          : message)
        : state.messages,
    };
  }
  if ("session_id" in action && state.sessionId && action.session_id !== state.sessionId) {
    return state;
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
    if (!action.turn_id && (action.metadata?.proactive || action.metadata?.notification)) {
      const id = action.message_id || String(action.metadata.message_id || `proactive-${state.messages.length}`);
      if (state.messages.some((message) => message.id === id)) return state;
      const source = String(action.metadata.source || "") as ChatMessage["source"];
      return {
        ...state,
        messages: [...state.messages, {
          id,
          role: "assistant",
          content: action.content,
          thinking: action.thinking ?? "",
          media: action.media ?? [],
          tools: [],
          streaming: false,
          proactive: Boolean(action.metadata.proactive),
          source,
          scheduledAt: String(action.metadata.scheduled_at || "") || undefined,
          timestamp: String(action.metadata.generated_at || "") || undefined,
        }],
      };
    }
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
  return state;
}

function createDraft(turnId: string): ChatMessage {
  return {
    id: turnId,
    turnId,
    role: "assistant",
    content: "",
    thinking: "",
    media: [],
    tools: [],
    streaming: true,
  };
}

function setTurnState(
  state: ChatState,
  sessionId: string,
  value: TurnRuntimeState,
): Record<string, TurnRuntimeState> {
  return { ...state.turnStates, [sessionId]: { ...value } };
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
    proactive: Boolean(row.proactive),
    source: row.proactive
      ? (String(row.metadata?.source || "proactive_conversation") as ChatMessage["source"])
      : undefined,
  }));
}

export function notificationRowsToMessages(rows: ProactiveNotificationRow[]): ChatMessage[] {
  // HTTP 边界仍需运行时校验；异常条目不能让 MessageView 因 content 缺失而崩溃。
  return rows.filter((row) => (
    typeof row?.id === "string" && typeof row?.content === "string"
  )).map((row) => ({
    id: row.id,
    role: "assistant",
    content: row.content,
    thinking: "",
    media: [],
    tools: [],
    streaming: false,
    source: row.source,
    scheduledAt: row.scheduled_at,
    timestamp: row.generated_at,
  }));
}

export function mergeTimeline(...groups: ChatMessage[][]): ChatMessage[] {
  return groups.flat().sort((left, right) => {
    const leftTime = Date.parse(left.timestamp || "");
    const rightTime = Date.parse(right.timestamp || "");
    if (Number.isNaN(leftTime) || Number.isNaN(rightTime)) return 0;
    return leftTime - rightTime;
  });
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
