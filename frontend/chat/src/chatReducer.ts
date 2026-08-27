import type { ChatAction, ChatMessage, ChatState, MessageRow, ProactiveNotificationRow, ToolActivity, TurnRuntimeState } from "./types";
import { reconcileMessages } from "./timeline";

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

function isTurnActive(status: TurnRuntimeState["status"]): boolean {
  return status === "submitting"
    || status === "queued"
    || status === "running"
    || status === "compacting";
}

export function reduceChatFrame(state: ChatState, action: ChatAction): ChatState {
  if (action.type === "ui.session.select") {
    const turn = state.turnStates[action.sessionId] ?? idleTurnState;
    const active = isTurnActive(turn.status);
    const cached = state.sessionMessages[action.sessionId];
    // HTTP 历史可能在发送确认前返回空结果；只要本地有快照，就不能用空历史覆盖它。
    let messages = action.messages;
    if (!action.replace && cached) {
      messages = active
        ? reconcileMessages([...action.messages, ...cached])
        : action.messages.length === 0 ? cached : action.messages;
    }
    if (!action.replace && isTurnActive(turn.status) && turn.turnId
      && !messages.some((message) => message.turnId === turn.turnId)) {
      messages = [...messages, createDraft(turn.turnId)];
    }
    messages = reconcileMessages(messages);
    return {
      ...state,
      sessionId: action.sessionId,
      activeTurnId: isTurnActive(turn.status) ? turn.turnId : "",
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
    const draftMessages = state.sessionId === "" ? state.messages : [];
    const draftTurn = state.turnStates[""];
    const turnStates = { ...state.turnStates };
    if (draftTurn) {
      turnStates[action.session_id] = { ...draftTurn };
      delete turnStates[""];
    }
    return {
      ...state,
      sessionId: action.session_id,
      activeTurnId: "",
      messages: draftMessages,
      sessionMessages: setSessionMessages(state, action.session_id, draftMessages),
      turnStates,
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
    const requestId = action.request_id || state.turnStates[action.session_id]?.requestId;
    const source = getSessionMessages(state, action.session_id);
    const userId = `user-${requestId}`;
    const existingUser = source.find((item) => (
      item.role === "user" && (item.turnId === action.turn_id || item.id === userId)
    ));
    const draft = createDraft(action.turn_id);
    const sessionMessages = [
      ...source.filter((item) => item.turnId !== action.turn_id && item.id !== userId),
      ...(existingUser ? [{ ...existingUser, turnId: action.turn_id }] : []),
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
  if (action.type === "context.compaction.started") {
    const current = state.turnStates[action.session_id] ?? idleTurnState;
    if (current.turnId && current.turnId !== action.turn_id) return state;
    const turnStates = setTurnState(state, action.session_id, {
      status: "compacting",
      queuePosition: null,
      turnId: action.turn_id,
      requestId: current.requestId,
    });
    return {
      ...state,
      activeTurnId: action.session_id === state.sessionId ? action.turn_id : state.activeTurnId,
      turnStates,
    };
  }
  if (action.type === "context.compaction.completed") {
    const current = state.turnStates[action.session_id] ?? idleTurnState;
    if (current.turnId !== action.turn_id) return state;
    return {
      ...state,
      turnStates: setTurnState(state, action.session_id, {
        ...current,
        status: "running",
      }),
    };
  }
  if (action.type === "context.compaction.failed") {
    const current = state.turnStates[action.session_id] ?? idleTurnState;
    if (current.turnId !== action.turn_id) return state;
    return {
      ...state,
      activeTurnId: action.session_id === state.sessionId ? "" : state.activeTurnId,
      turnStates: setTurnState(state, action.session_id, idleTurnState),
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
        ? {
          ...message,
          streaming: false,
          status: "interrupted",
          thinkingStatus: message.thinking ? "interrupted" : message.thinkingStatus,
        }
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
    const snapshotTimestamp = new Date().toISOString();
    const current = action.session_id === state.sessionId;
    const previousTurn = state.turnStates[action.session_id];
    const turnStates = setTurnState(state, action.session_id, {
      // 重连快照可能紧跟在压缩 started 帧之后到达；同一 Turn 仍处于
      // checkpoint 等待时，不能让普通 running 快照覆盖前端状态提示。
      status: previousTurn?.turnId === action.turn_id && previousTurn.status === "compacting"
        ? "compacting"
        : "running",
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
      timestamp: snapshotTimestamp,
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
      thinkingStatus: (action.thinking || existingAssistant?.thinking)
        ? "running"
        : existingAssistant?.thinkingStatus,
      media: existingAssistant?.media ?? [],
      tools: mergeTools(existingAssistant?.tools ?? [], incomingTools),
      streaming: true,
      timestamp: existingAssistant?.timestamp ?? snapshotTimestamp,
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
      thinkingStatus: message.thinkingStatus === "completed" ? "completed" : "running",
    }));
  }
  if (action.type === "react.tool.started") {
    return updateSessionTurn(state, action.session_id, action.turn_id, (message) => ({
      ...message,
      thinkingStatus: message.thinking ? "running" : message.thinkingStatus,
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
      thinkingStatus: (action.thinking || message.thinking) ? "completed" : message.thinkingStatus,
      media: action.media ?? message.media,
      streaming: false,
      timestamp: String(action.metadata?.generated_at || "") || message.timestamp,
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
    timestamp: new Date().toISOString(),
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
  return rows.filter((row) => row.role === "user" || row.role === "assistant").map((row) => {
    const interrupted = row.status === "interrupted" && row.role === "assistant";
    const thinking = row.proactive ? "" : interrupted
      ? (row.interrupted_display_reasoning ?? "")
      : (row.reasoning_content ?? "");
    const content = interrupted ? (row.interrupted_display_content ?? "") : row.content;
    return {
      id: row.id,
      seq: typeof row.seq === "number" ? row.seq : undefined,
      role: row.role === "user" ? "user" : "assistant",
      content,
      thinking,
      thinkingStatus: thinking ? interrupted
        ? "interrupted"
        : Boolean(row.metadata?.running) ? "running" : "completed"
        : undefined,
      media: Array.isArray(row.media) ? row.media : [],
      tools: row.proactive ? [] : toolChainToActivities(row.tool_chain),
      turnId: row.turn_id,
      streaming: Boolean(row.metadata?.running) && row.role === "assistant",
      status: row.status,
      timestamp: row.timestamp,
      proactive: Boolean(row.proactive),
      source: row.proactive
        ? (String(row.metadata?.source || "proactive_conversation") as ChatMessage["source"])
        : undefined,
    };
  });
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
    const leftMissing = Number.isNaN(leftTime);
    const rightMissing = Number.isNaN(rightTime);
    if (leftMissing && rightMissing) return 0;
    if (leftMissing) return 1;
    if (rightMissing) return -1;
    return leftTime - rightTime;
  });
}

function toolChainToActivities(chain: MessageRow["tool_chain"]): ToolActivity[] {
  return (chain ?? []).flatMap((group) => (group.calls ?? []).map((call) => ({
    callId: String(call.call_id ?? ""),
    name: String(call.name ?? "tool"),
    status: call.status === "error"
      ? "error"
      : call.status === "running"
        ? "running"
        : call.status === "interrupted"
          ? "interrupted"
          : "completed",
    arguments: call.arguments,
    resultPreview: String(call.result ?? ""),
  })));
}
