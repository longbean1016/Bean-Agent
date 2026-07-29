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
  sessionMessages: {},
  error: "",
  turnStates: {},
};

export function reduceChatFrame(state: ChatState, action: ChatAction): ChatState {
  if (action.type === "ui.session.select") {
    const turn = state.turnStates[action.sessionId] ?? idleTurnState;
    const active = turn.status === "submitting" || turn.status === "queued" || turn.status === "running";
    const cached = state.sessionMessages[action.sessionId];
    // HTTP 历史可能在发送确认前返回空结果；只要本地有快照，就不能用空历史覆盖它。
    let messages = cached && (active || action.messages.length === 0) ? cached : action.messages;
    if (turn.status === "running" && turn.turnId
      && !messages.some((message) => message.turnId === turn.turnId)) {
      messages = [...messages, createDraft(turn.turnId)];
    }
    return {
      ...state,
      sessionId: action.sessionId,
      activeTurnId: turn.status === "running" ? turn.turnId : "",
      messages,
      sessionMessages: setSessionMessages(state, action.sessionId, messages),
      error: "",
    };
  }
  if (action.type === "ui.user.append") {
    const messages = [...state.messages, action.message];
    return {
      ...state,
      messages,
      sessionMessages: setSessionMessages(state, state.sessionId, messages),
      error: "",
    };
  }
  if (action.type === "ui.turn.submitted") {
    return {
      ...state,
      turnStates: setTurnState(state, action.sessionId, {
        status: "submitting",
        queuePosition: null,
        turnId: "",
        requestId: action.requestId,
      }),
    };
  }
  if (action.type === "ui.error.clear") return { ...state, error: "" };
  if (action.type === "session.created") {
    return {
      ...state,
      sessionId: action.session_id,
      activeTurnId: "",
      messages: [],
      sessionMessages: setSessionMessages(state, action.session_id, []),
      error: "",
    };
  }
  if (action.type === "session.subscribed") return state;
  if (action.type === "error") {
    const rejected = action.code === "queue_full" || action.code === "session_busy" || action.code === "closed";
    if (!rejected || !action.session_id) return { ...state, error: action.message };
    const current = action.session_id === state.sessionId;
    const sessionMessages = getSessionMessages(state, action.session_id)
      .filter((message) => message.id !== `user-${action.request_id}`);
    return {
      ...state,
      activeTurnId: current ? "" : state.activeTurnId,
      messages: current ? sessionMessages : state.messages,
      sessionMessages: setSessionMessages(state, action.session_id, sessionMessages),
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
    const draft = createDraft(action.turn_id);
    const sessionMessages = [
      ...getSessionMessages(state, action.session_id).filter((item) => item.turnId !== action.turn_id),
      draft,
    ];
    if (state.sessionId && action.session_id !== state.sessionId) {
      return {
        ...state,
        turnStates,
        sessionMessages: setSessionMessages(state, action.session_id, sessionMessages),
      };
    }
    return {
      ...state,
      sessionId: action.session_id,
      activeTurnId: action.turn_id,
      turnStates,
      messages: sessionMessages,
      sessionMessages: setSessionMessages(state, action.session_id, sessionMessages),
      error: "",
    };
  }
  if (action.type === "message.final" && action.turn_id) {
    const nextState = {
      ...state,
      turnStates: setTurnState(state, action.session_id, idleTurnState),
    };
    state = nextState;
  }
  if (action.type === "turn.interrupted") {
    const current = action.session_id === state.sessionId;
    const interruptedTurnId = action.turn_id
      || state.turnStates[action.session_id]?.turnId
      || (current ? state.activeTurnId : "");
    const sessionMessages = interruptedTurnId
      ? getSessionMessages(state, action.session_id).map((message) => message.turnId === interruptedTurnId
        ? { ...message, streaming: false, status: "interrupted" }
        : message)
      : getSessionMessages(state, action.session_id);
    const nextState = {
      ...state,
      activeTurnId: current ? "" : state.activeTurnId,
      turnStates: setTurnState(state, action.session_id, idleTurnState),
      sessionMessages: setSessionMessages(state, action.session_id, sessionMessages),
    };
    if (!current) return nextState;
    return {
      ...nextState,
      messages: sessionMessages,
    };
  }
  if (action.type === "turn.snapshot") {
    const current = action.session_id === state.sessionId;
    const turnStates = setTurnState(state, action.session_id, {
      status: "running",
      queuePosition: null,
      turnId: action.turn_id,
      requestId: action.request_id ?? "",
    });
    const source = getSessionMessages(state, action.session_id);
    const userId = action.request_id ? `user-${action.request_id}` : `user-${action.turn_id}`;
    const user: ChatMessage = {
      id: userId,
      role: "user",
      content: action.user_message,
      thinking: "",
      media: action.user_media ?? [],
      tools: [],
      turnId: action.turn_id,
      streaming: false,
    };
    const incomingTools = action.tools.map((tool) => ({
      callId: tool.call_id,
      name: tool.name,
      status: tool.status === "error" ? "error" as const : tool.status === "running" ? "running" as const : "completed" as const,
      arguments: tool.arguments,
      resultPreview: tool.result_preview,
    }));
    const existingUser = source.find((message) => (
      message.role === "user" && (message.turnId === action.turn_id || message.id === userId)
    ));
    const existingAssistant = source.find((message) => message.role === "assistant" && message.turnId === action.turn_id);
    const assistant: ChatMessage = {
      ...(existingAssistant ?? createDraft(action.turn_id)),
      id: action.turn_id,
      turnId: action.turn_id,
      role: "assistant",
      content: longestText(existingAssistant?.content ?? "", action.content ?? ""),
      thinking: longestText(existingAssistant?.thinking ?? "", action.thinking ?? ""),
      media: existingAssistant?.media ?? [],
      tools: mergeTools(existingAssistant?.tools ?? [], incomingTools),
      streaming: true,
    };
    const withoutTurn = source.filter((message) => message.turnId !== action.turn_id && message.id !== userId);
    const restoredUser = existingUser ? {
      ...existingUser,
      content: existingUser.content || user.content,
      media: existingUser.media.length ? existingUser.media : user.media,
      turnId: action.turn_id,
      streaming: false,
    } : user;
    const messages = [...withoutTurn, restoredUser, assistant];
    return {
      ...state,
      activeTurnId: current ? action.turn_id : state.activeTurnId,
      turnStates,
      messages: current ? messages : state.messages,
      sessionMessages: setSessionMessages(state, action.session_id, messages),
      error: current ? "" : state.error,
    };
  }
  if (action.type === "answer.delta") {
    return updateSessionTurn(state, action.session_id, action.turn_id, (message) => ({
      ...message,
      content: message.content + action.delta,
      streaming: true,
    }));
  }
  if (action.type === "react.thinking.delta") {
    return updateSessionTurn(state, action.session_id, action.turn_id, (message) => ({
      ...message,
      thinking: message.thinking + action.delta,
      streaming: true,
    }));
  }
  if (action.type === "react.tool.started") {
    return updateSessionTurn(state, action.session_id, action.turn_id, (message) => ({
      ...message,
      tools: mergeTools(message.tools, [{
        callId: action.call_id,
        name: action.tool_name,
        status: "running",
        arguments: action.arguments,
        resultPreview: "",
      }]),
    }));
  }
  if (action.type === "react.tool.completed") {
    return updateSessionTurn(state, action.session_id, action.turn_id, (message) => ({
      ...message,
      tools: mergeTools(message.tools, [{
        callId: action.call_id,
        name: action.tool_name,
        status: action.status === "error" ? "error" : "completed",
        arguments: undefined,
        resultPreview: action.result_preview,
      }]),
    }));
  }
  if (action.type === "message.final") {
    if (!action.turn_id && (action.metadata?.proactive || action.metadata?.notification)) {
      const id = action.message_id || String(action.metadata.message_id || `proactive-${state.messages.length}`);
      const existing = getSessionMessages(state, action.session_id);
      if (existing.some((message) => message.id === id)) return state;
      const source = String(action.metadata.source || "") as ChatMessage["source"];
      const proactiveMessage: ChatMessage = {
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
        };
      const messages = [...existing, proactiveMessage];
      return {
        ...state,
        messages: action.session_id === state.sessionId ? messages : state.messages,
        sessionMessages: setSessionMessages(state, action.session_id, messages),
      };
    }
    // final 是服务端的权威快照，必须覆盖草稿，不能继续追加 delta。
    const next = updateSessionTurn(state, action.session_id, action.turn_id, (message) => ({
      ...message,
      content: action.content || message.content,
      thinking: action.thinking || message.thinking,
      media: action.media ?? message.media,
      streaming: false,
    }));
    return {
      ...next,
      activeTurnId: action.session_id === state.sessionId ? "" : next.activeTurnId,
    };
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

function getSessionMessages(state: ChatState, sessionId: string): ChatMessage[] {
  if (Object.prototype.hasOwnProperty.call(state.sessionMessages, sessionId)) {
    return state.sessionMessages[sessionId];
  }
  return sessionId === state.sessionId ? state.messages : [];
}

function setSessionMessages(
  state: ChatState,
  sessionId: string,
  messages: ChatMessage[],
): Record<string, ChatMessage[]> {
  if (!sessionId) return state.sessionMessages;
  return { ...state.sessionMessages, [sessionId]: messages };
}

function longestText(current: string, incoming: string): string {
  return current.length > incoming.length ? current : incoming;
}

function mergeTools(current: ToolActivity[], incoming: ToolActivity[]): ToolActivity[] {
  const merged = [...current];
  for (const tool of incoming) {
    const index = merged.findIndex((item) => item.callId === tool.callId);
    if (index < 0) {
      merged.push(tool);
      continue;
    }
    const existing = merged[index];
    if ((existing.status === "completed" || existing.status === "error") && tool.status === "running") {
      continue;
    }
    merged[index] = {
      ...existing,
      ...tool,
      arguments: tool.arguments === undefined ? existing.arguments : tool.arguments,
      resultPreview: tool.resultPreview || existing.resultPreview,
    };
  }
  return merged;
}

function updateSessionTurn(
  state: ChatState,
  sessionId: string,
  turnId: string,
  updater: (message: ChatMessage) => ChatMessage,
): ChatState {
  const source = getSessionMessages(state, sessionId);
  const index = source.findIndex((message) => message.role === "assistant" && message.turnId === turnId);
  if (index < 0) return state;
  const messages = [...source];
  messages[index] = updater(messages[index]);
  return {
    ...state,
    messages: sessionId === state.sessionId ? messages : state.messages,
    sessionMessages: setSessionMessages(state, sessionId, messages),
  };
}

export function rowsToMessages(rows: MessageRow[]): ChatMessage[] {
  return rows.filter((row) => row.role === "user" || row.role === "assistant").map((row) => ({
    id: row.id,
    role: row.role === "user" ? "user" : "assistant",
    content: row.content,
    thinking: row.proactive ? "" : (row.reasoning_content ?? ""),
    media: Array.isArray(row.media) ? row.media : [],
    tools: row.proactive ? [] : toolChainToActivities(row.tool_chain),
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
