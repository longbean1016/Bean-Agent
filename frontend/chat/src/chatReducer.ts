import type { ApprovalRequest, ChatAction, ChatMessage, ChatState, ContextUsage, MessageRow, ModelAdapterId, ProactiveNotificationRow, ResolvedApproval, SessionUsage, ToolActivity, ToolStatus, TurnRuntimeState } from "./types";
import { reconcileMessages } from "./timeline";
import { appendProcessDelta, readPresentation } from "./turnPresentation";

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
  approvalRequests: {},
  resolvedApprovals: {},
  turnStates: {},
  contextUsage: {},
  sessionUsage: {},
};

function isTurnActive(status: TurnRuntimeState["status"]): boolean {
  return status === "submitting"
    || status === "queued"
    || status === "running"
    || status === "compacting";
}

function isActiveTurnMatch(state: ChatState, sessionId: string, turnId: string): boolean {
  if (!sessionId || !turnId) return false;
  const runtime = state.turnStates[sessionId];
  return runtime?.turnId === turnId
    || (sessionId === state.sessionId && state.activeTurnId === turnId);
}

function legacyFrameTurnId(state: ChatState, sessionId: string, requestId?: string): string {
  const runtime = state.turnStates[sessionId];
  if (!runtime || !isTurnActive(runtime.status)) return "";
  // 旧帧可能没有 turn_id，但只要带有 request_id，就不能让上一轮的迟到帧
  // 覆盖当前请求；没有 request_id 时保留旧版 queued/running 兼容回退。
  if (requestId && runtime.requestId && requestId !== runtime.requestId) return "";
  return runtime.turnId || (sessionId === state.sessionId ? state.activeTurnId : "");
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
    const requestSessionId = Object.entries(state.turnStates)
      .find(([, turn]) => turn.requestId && turn.requestId === action.request_id)?.[0];
    const rejected = action.code === "queue_full" || action.code === "session_busy" || action.code === "closed";
    const failedSessionId = rejected ? action.session_id : requestSessionId;
    if (!failedSessionId) return { ...state, error: action.message };
    // 首轮创建 Session 后错误帧可能省略 session_id，必须通过 request id 找回已迁移的
    // submitting 状态，否则输入框会永久停留在“停止”按钮。
    const current = failedSessionId === state.sessionId;
    const sessionMessages = getSessionMessages(state, failedSessionId)
      .filter((message) => message.id !== `user-${action.request_id}`);
    return {
      ...state,
      activeTurnId: current ? "" : state.activeTurnId,
      messages: current ? sessionMessages : state.messages,
      sessionMessages: setSessionMessages(state, failedSessionId, sessionMessages),
      error: current ? action.message : state.error,
      turnStates: setTurnState(state, failedSessionId, idleTurnState),
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
    // tool.started 可能先于 turn.started 到达；保留已经收集到的工具行，避免
    // 服务端确认帧把乱序事件创建的草稿覆盖掉。
    const existingAssistant = source.find((item) => item.role === "assistant" && item.turnId === action.turn_id);
    if (existingAssistant && isClosedMessage(existingAssistant)) {
      // 迟到的 started 不能重新激活已经收到 final/error 的同一 Turn。
      return state;
    }
    const draft = existingAssistant
      ? { ...existingAssistant, streaming: existingAssistant.streaming ?? true }
      : createDraft(action.turn_id);
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
  if (action.type === "context.usage.reset") {
    if (!Object.prototype.hasOwnProperty.call(state.contextUsage, action.session_id)) return state;
    const contextUsage = { ...state.contextUsage };
    delete contextUsage[action.session_id];
    return { ...state, contextUsage };
  }
  if (action.type === "context.usage.updated") {
    const currentTurn = state.turnStates[action.session_id];
    // 同一会话只能有一个运行中的 Turn；旧 Turn 的迟到估算不能覆盖新 Turn 的占用。
    if (action.turn_id && currentTurn?.turnId && currentTurn.turnId !== action.turn_id) return state;
    // 新协议的 heuristic 事件只用于 gate 和明细，不代表 Provider 已报告
    // pressure；没有它时不创建圆圈状态，避免新会话打开就显示假占用。
    if (action.model_runtime_id && action.pressure_tokens === undefined) return state;
    const pressureTokens = action.pressure_tokens === undefined
      ? undefined
      : Math.max(0, Number(action.pressure_tokens) || 0);
    const projectedTokens = action.projected_tokens === undefined
      ? undefined
      : Math.max(0, Number(action.projected_tokens) || 0);
    const usage: ContextUsage = {
      turnId: action.turn_id,
      usedTokens: projectedTokens ?? pressureTokens ?? Math.max(0, Number(action.used_tokens) || 0),
      pressureTokens,
      projectedTokens,
      surfaceTokens: action.surface_tokens === undefined ? undefined : Math.max(0, Number(action.surface_tokens) || 0),
      systemTokens: action.system_tokens === undefined ? undefined : Math.max(0, Number(action.system_tokens) || 0),
      toolsTokens: action.tools_tokens === undefined ? undefined : Math.max(0, Number(action.tools_tokens) || 0),
      messageTokens: action.message_tokens === undefined ? undefined : Math.max(0, Number(action.message_tokens) || 0),
      asOfSeq: action.as_of_seq === undefined ? undefined : Math.max(0, Number(action.as_of_seq) || 0),
      modelRuntimeId: action.model_runtime_id,
      model: action.model,
      contextWindow: Math.max(0, Number(action.context_window) || 0),
      softLimitTokens: Math.max(0, Number(action.soft_limit_tokens) || 0),
      hardInputTokens: Math.max(0, Number(action.hard_input_tokens) || 0),
      contextWindowSource: String(action.context_window_source || "unknown"),
      estimateSource: String(action.estimate_source || "heuristic"),
      breakdown: {
        system_prompt_tokens: Math.max(0, Number(action.breakdown.system_prompt_tokens) || 0),
        tools_tokens: Math.max(0, Number(action.breakdown.tools_tokens) || 0),
        conversation_tokens: Math.max(0, Number(action.breakdown.conversation_tokens) || 0),
        overhead_tokens: Math.max(0, Number(action.breakdown.overhead_tokens) || 0),
      },
      sections: Array.isArray(action.sections) ? action.sections.map((section) => ({
        name: String(section.name || ""),
        estimated_tokens: Math.max(0, Number(section.estimated_tokens) || 0),
        static: Boolean(section.static),
        cache_hit: Boolean(section.cache_hit),
      })) : [],
    };
    return {
      ...state,
      contextUsage: { ...state.contextUsage, [action.session_id]: usage },
    };
  }
  if (action.type === "session.usage.updated") {
    const clean = (value: number) => Math.max(0, Number(value) || 0);
    const sessionUsage: SessionUsage = {
      totalUncachedInputTokens: clean(action.total_uncached_input_tokens),
      totalCacheReadTokens: clean(action.total_cache_read_tokens),
      totalCacheWriteTokens: clean(action.total_cache_write_tokens),
      totalInputTokens: clean(action.total_input_tokens),
      cacheHitRate: action.cache_hit_rate === null ? null : Math.max(0, Number(action.cache_hit_rate) || 0),
      totalOutputTokens: clean(action.total_output_tokens),
    };
    return {
      ...state,
      sessionUsage: { ...state.sessionUsage, [action.session_id]: sessionUsage },
    };
  }
  if (action.type === "turn.interrupted") {
    const current = action.session_id === state.sessionId;
    const interruptedTurnId = action.turn_id
      || state.turnStates[action.session_id]?.turnId
      || (current ? state.activeTurnId : "");
    // 旧协议的取消帧可能没有 turn_id；此时只能把当前会话的 queued/running
    // Turn 作为目标，不能因为空 turn_id 把排队状态永久留在 queued。
    const interruptedOwnsActiveTurn = action.turn_id
      ? isActiveTurnMatch(state, action.session_id, interruptedTurnId)
      : Boolean(state.turnStates[action.session_id] && isTurnActive(state.turnStates[action.session_id].status));
    const interruptedAt = action.ended_at || new Date().toISOString();
    const interruptionToolStatus = normalizeToolStatus(action.status) === "cancelled"
      ? "cancelled"
      : normalizeToolStatus(action.status) === "expired" ? "expired" : "interrupted";
    const source = getSessionMessages(state, action.session_id);
    const turnUser = interruptedTurnId
      ? source.find((message) => message.role === "user" && message.turnId === interruptedTurnId)
      : undefined;
    const sessionMessages = interruptedTurnId
      ? source.map((message) => {
        if (message.turnId !== interruptedTurnId || message.role !== "assistant") return message;
        const durationMs = message.durationMs
          ?? normalizeDuration(action.duration_ms)
          ?? (isRuntimeMessage(turnUser) ? elapsedDurationMs(turnUser?.timestamp, interruptedAt) : undefined);
        const tools = message.tools.map((tool) => interruptTool(tool, interruptedAt, interruptionToolStatus));
        // 已经收到 completed/error 的助手消息不能因迟到的 interrupted 帧回退；
        // 但仍要把其中尚未结束的工具收敛为中断，避免 UI 永久显示执行中。
        const messageTerminal = isTerminalMessageStatus(message.status)
          || (message.streaming === false && !message.status);
        return {
          ...message,
          streaming: false,
          status: messageTerminal ? message.status : "interrupted",
          thinkingStatus: message.thinking && !messageTerminal ? "interrupted" : message.thinkingStatus,
          tools,
          ...(durationMs === undefined ? {} : { durationMs }),
        };
      })
      : source;
    const nextState = {
      ...state,
      activeTurnId: current && interruptedOwnsActiveTurn ? "" : state.activeTurnId,
      turnStates: interruptedOwnsActiveTurn
        ? setTurnState(state, action.session_id, idleTurnState)
        : state.turnStates,
      sessionMessages: setSessionMessages(state, action.session_id, sessionMessages),
      approvalRequests: interruptedTurnId
        ? removeApprovalRequestsForTurn(state, action.session_id, interruptedTurnId)
        : state.approvalRequests,
    };
    if (!current) return nextState;
    return {
      ...nextState,
      messages: sessionMessages,
    };
  }
  if (action.type === "turn.snapshot") {
    const snapshotTimestamp = new Date().toISOString();
    const startedAt = String(action.started_at || "") || snapshotTimestamp;
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
    const existingAssistant = source.find((message) => message.role === "assistant" && message.turnId === action.turn_id);
    if (existingAssistant && isClosedMessage(existingAssistant)) {
      // 重连快照可能晚于 final；终态消息不应重新进入 streaming/running。
      return state;
    }
    const userId = action.request_id ? `user-${action.request_id}` : `user-${action.turn_id}`;
    const user: ChatMessage = {
      id: userId,
      role: "user",
      content: String(action.user_message ?? ""),
      thinking: "",
      media: action.user_media ?? [],
      tools: [],
      turnId: action.turn_id,
      streaming: false,
      timestamp: startedAt,
    };
    const incomingTools = (action.tools ?? []).map((tool) => decorateToolWithApproval(state, action.session_id, action.turn_id, {
      callId: tool.call_id,
      name: tool.name,
      status: normalizeToolStatus(tool.status),
      arguments: tool.arguments,
      resultPreview: String(tool.result_preview ?? ""),
      ...(tool.started_at ? { startedAt: tool.started_at } : {}),
      ...(tool.ended_at ? { endedAt: tool.ended_at } : {}),
      ...(normalizeOptionalDuration(tool.duration_ms) !== undefined ? { durationMs: normalizeOptionalDuration(tool.duration_ms) } : {}),
      ...(tool.approval_id ? { approvalId: tool.approval_id } : {}),
      ...(tool.approval_state ? { approvalState: normalizeApprovalState(tool.approval_state) } : {}),
      ...(tool.approval_requested_at ? { approvalRequestedAt: tool.approval_requested_at } : {}),
      ...(tool.approval_resolved_at ? { approvalResolvedAt: tool.approval_resolved_at } : {}),
      ...(normalizeOptionalDuration(tool.approval_wait_ms) !== undefined ? { approvalWaitMs: normalizeOptionalDuration(tool.approval_wait_ms) } : {}),
      ...(normalizeOptionalDuration(tool.execution_ms) !== undefined ? { executionMs: normalizeOptionalDuration(tool.execution_ms) } : {}),
      ...(normalizeOptionalDuration(tool.group_duration_ms) !== undefined ? { groupDurationMs: normalizeOptionalDuration(tool.group_duration_ms) } : {}),
      ...(tool.result_kind ? { resultKind: tool.result_kind } : {}),
      ...(tool.is_truncated === undefined || tool.is_truncated === null ? {} : { isTruncated: Boolean(tool.is_truncated) }),
      ...(normalizeOptionalNumber(tool.exit_code) !== undefined ? { exitCode: normalizeOptionalNumber(tool.exit_code) } : {}),
      ...(tool.error_code ? { errorCode: tool.error_code } : {}),
    }));
    const existingUser = source.find((message) => (
      message.role === "user" && (message.turnId === action.turn_id || message.id === userId)
    ));
    const assistant: ChatMessage = {
      ...(existingAssistant ?? createDraft(action.turn_id)),
      id: action.turn_id,
      turnId: action.turn_id,
      role: "assistant",
      content: longestText(existingAssistant?.content ?? "", String(action.content ?? "")),
      thinking: longestText(existingAssistant?.thinking ?? "", String(action.thinking ?? "")),
      thinkingStatus: (action.thinking || existingAssistant?.thinking)
        ? "running"
        : existingAssistant?.thinkingStatus,
      media: existingAssistant?.media ?? [],
      tools: mergeTools(existingAssistant?.tools ?? [], incomingTools),
      presentation: readPresentation(action.presentation) ?? existingAssistant?.presentation,
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
  if (action.type === "turn.presentation") {
    const presentation = readPresentation(action.presentation);
    if (!presentation) return state;
    return updateSessionTurn(state, action.session_id, action.turn_id, (message) =>
      isClosedMessage(message) ? message : { ...message, presentation, streaming: true });
  }
  if (action.type === "answer.delta") {
    return updateSessionTurn(state, action.session_id, action.turn_id, (message) => {
      if (isClosedMessage(message)) return message;
      return {
        ...message,
        content: message.content + action.delta,
        presentation: appendProcessDelta(message.presentation, action.part_id, "text", action.delta),
        streaming: true,
      };
    });
  }
  if (action.type === "react.thinking.delta") {
    return updateSessionTurn(state, action.session_id, action.turn_id, (message) => {
      if (isClosedMessage(message)) return message;
      return {
        ...message,
        thinking: message.thinking + action.delta,
        presentation: appendProcessDelta(message.presentation, action.part_id, "thinking", action.delta),
        streaming: true,
        thinkingStatus: message.thinkingStatus === "completed" ? "completed" : "running",
      };
    });
  }
  if (action.type === "approval.requested") {
    const requestedAt = action.approval.requested_at || action.approval.created_at;
    const resolved = state.resolvedApprovals?.[action.session_id]?.[action.approval.id];
    // resolved 可能在重连 replay 中先于 requested 到达；不要把已结束的审批
    // 重新放回等待队列，只把终态补到已经存在的工具行。
    if (resolved && resolved.session_id === action.session_id) {
      // 某些旧服务端回执只携带 approval_id；requested replay 才带有
      // turn/call/created_at。先补齐关联信息，后续无 approval_id 的工具帧
      // 才能通过 turn_id + call_id 找到这条已决议记录。
      const enrichedResolution = mergeResolvedApproval(resolved, {
        ...resolved,
        turn_id: resolved.turn_id || action.approval.turn_id,
        call_id: resolved.call_id || action.approval.call_id,
        requested_at: resolved.requested_at || requestedAt,
      });
      const next = updateToolApproval(state, action.session_id, {
        turnId: enrichedResolution.turn_id || action.approval.turn_id,
        callId: enrichedResolution.call_id || action.approval.call_id,
        approvalId: action.approval.id,
      }, (tool) => applyResolvedApproval(tool, enrichedResolution));
      return {
        ...next,
        resolvedApprovals: rememberResolvedApproval(state.resolvedApprovals, enrichedResolution),
      };
    }
    const source = getSessionMessages(state, action.session_id);
    const terminalToolMatched = source.some((message) => message.role === "assistant"
      && (!action.approval.turn_id || message.turnId === action.approval.turn_id)
      && message.tools.some((tool) => (
        (tool.callId === action.approval.call_id || tool.approvalId === action.approval.id)
         && isKnownTerminalToolStatus(tool.status)
      )));
    const next = updateToolApproval(state, action.session_id, {
      turnId: action.approval.turn_id,
      callId: action.approval.call_id,
      approvalId: action.approval.id,
    }, (tool) => isKnownTerminalToolStatus(tool.status)
      // 终态优先：迟到的 requested 只能补关联时间，不能把工具重新打开为 pending。
      ? {
          ...tool,
          approvalId: action.approval.id,
          ...(requestedAt ? { approvalRequestedAt: tool.approvalRequestedAt || requestedAt } : {}),
        }
      : {
          ...tool,
          approvalId: action.approval.id,
          approvalState: "pending",
          ...(requestedAt ? { approvalRequestedAt: requestedAt } : {}),
        });
    // 如果已经有终态工具，审批请求本身也是迟到 replay；不要生成没有对应
    // 工具行的可点击卡片，等待服务端的 resolved/历史终态完成收敛。
    if (terminalToolMatched) return next;
    return {
      ...next,
      approvalRequests: {
        ...(state.approvalRequests ?? {}),
        [action.session_id]: {
          ...(state.approvalRequests?.[action.session_id] ?? {}),
          [action.approval.id]: action.approval,
        },
      },
    };
  }
  if (action.type === "approval.resolved") {
    const remembered = state.approvalRequests?.[action.session_id]?.[action.approval_id];
    const resolvedState = normalizeResolutionState(action.state, action.decision);
    const resolvedAt = action.decided_at || new Date().toISOString();
    const incomingResolution: ResolvedApproval = {
      id: action.approval_id,
      session_id: action.session_id,
      turn_id: action.turn_id || remembered?.turn_id,
      call_id: action.call_id || remembered?.call_id,
      decision: normalizeApprovalDecision(action.decision, resolvedState),
      state: resolvedState,
      requested_at: remembered?.requested_at || remembered?.created_at,
      decided_at: resolvedAt,
      ...(action.error_code ? { error_code: action.error_code } : {}),
    };
    const resolution = mergeResolvedApproval(
      state.resolvedApprovals?.[action.session_id]?.[action.approval_id],
      incomingResolution,
    );
    const next = updateToolApproval(state, action.session_id, {
      turnId: resolution.turn_id,
      callId: resolution.call_id,
      approvalId: action.approval_id,
    }, (tool) => applyResolvedApproval(tool, resolution));
    const approvalRequests = { ...(state.approvalRequests ?? {}) };
    const sessionRequests = { ...(approvalRequests[action.session_id] ?? {}) };
    delete sessionRequests[action.approval_id];
    if (Object.keys(sessionRequests).length) approvalRequests[action.session_id] = sessionRequests;
    else delete approvalRequests[action.session_id];
    return {
      ...next,
      approvalRequests,
      resolvedApprovals: rememberResolvedApproval(state.resolvedApprovals, resolution),
    };
  }
  if (action.type === "react.tool.started") {
    const rememberedApproval = findPendingApproval(state, action.session_id, action.turn_id, action.call_id, action.approval_id);
    const resolvedApproval = findResolvedApproval(state, action.session_id, action.turn_id, action.call_id, action.approval_id);
    const approvalId = action.approval_id || rememberedApproval?.id || resolvedApproval?.id;
    const incoming = decorateToolWithApproval(state, action.session_id, action.turn_id, {
      callId: action.call_id,
      name: action.tool_name,
      status: "running",
      arguments: action.arguments,
      resultPreview: "",
      ...(action.started_at ? { startedAt: action.started_at } : {}),
      ...(action.approval_requested_at ? { approvalRequestedAt: action.approval_requested_at } : {}),
      ...(approvalId ? { approvalId } : {}),
      ...(action.approval_state ? { approvalState: normalizeApprovalState(action.approval_state) } : {}),
    });
    return updateSessionTurn(state, action.session_id, action.turn_id, (message) => ({
      ...message,
      thinkingStatus: message.thinking ? "running" : message.thinkingStatus,
      tools: mergeTools(message.tools, [incoming]),
    }), true);
  }
  if (action.type === "react.tool.completed") {
    const incoming = decorateToolWithApproval(state, action.session_id, action.turn_id, {
      callId: action.call_id,
      name: action.tool_name,
      status: normalizeToolStatus(action.status),
      arguments: undefined,
      resultPreview: action.result_preview,
      ...(action.started_at ? { startedAt: action.started_at } : {}),
      ...(action.ended_at ? { endedAt: action.ended_at } : {}),
      ...(normalizeOptionalDuration(action.duration_ms) !== undefined ? { durationMs: normalizeOptionalDuration(action.duration_ms) } : {}),
      ...(action.approval_requested_at ? { approvalRequestedAt: action.approval_requested_at } : {}),
      ...(action.approval_resolved_at ? { approvalResolvedAt: action.approval_resolved_at } : {}),
      ...(normalizeOptionalDuration(action.approval_wait_ms) !== undefined ? { approvalWaitMs: normalizeOptionalDuration(action.approval_wait_ms) } : {}),
      ...(normalizeOptionalDuration(action.execution_ms) !== undefined ? { executionMs: normalizeOptionalDuration(action.execution_ms) } : {}),
      ...(normalizeOptionalDuration(action.group_duration_ms) !== undefined ? { groupDurationMs: normalizeOptionalDuration(action.group_duration_ms) } : {}),
      ...(action.result_kind ? { resultKind: action.result_kind } : {}),
      ...(action.is_truncated === undefined ? {} : { isTruncated: action.is_truncated }),
      ...(normalizeOptionalNumber(action.exit_code) !== undefined ? { exitCode: normalizeOptionalNumber(action.exit_code) } : {}),
      ...(action.error_code ? { errorCode: action.error_code } : {}),
    });
    return updateSessionTurn(state, action.session_id, action.turn_id, (message) => ({
      ...message,
      tools: mergeTools(message.tools, [incoming]),
    }), true);
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
          durationMs: durationFromMetadata(action.metadata),
        };
      const messages = [...existing, proactiveMessage];
      return {
        ...state,
        messages: action.session_id === state.sessionId ? messages : state.messages,
        sessionMessages: setSessionMessages(state, action.session_id, messages),
      };
    }
    // final 是服务端的权威快照，必须覆盖草稿，不能继续追加 delta。
    // 迟到的旧 Turn final 不能清空同一会话已经开始的新 Turn；只有当
    // final 对应当前运行态时才收敛 turnStates，历史/后台 Turn 仍可正常落行。
    const finalTurnId = action.turn_id || legacyFrameTurnId(
      state,
      action.session_id,
      action.request_id,
    );
    const finalOwnsActiveTurn = Boolean(finalTurnId)
      && isActiveTurnMatch(state, action.session_id, finalTurnId);
    const source = getSessionMessages(state, action.session_id);
    const turnUser = source.find((message) => message.role === "user" && message.turnId === finalTurnId);
    const finalReceivedAt = new Date().toISOString();
    const metadataDuration = durationFromMetadata(action.metadata);
    const next = updateSessionTurn(state, action.session_id, finalTurnId, (message) => {
      const timestamp = String(action.metadata?.generated_at || "")
        || (message.streaming ? finalReceivedAt : message.timestamp);
      const durationMs = metadataDuration
        ?? message.durationMs
        ?? (isRuntimeMessage(turnUser) ? elapsedDurationMs(turnUser?.timestamp, timestamp) : undefined);
      return {
        ...message,
        content: action.content || message.content,
        presentation: readPresentation(action.metadata?.presentation) ?? message.presentation,
        thinking: action.thinking || message.thinking,
        thinkingStatus: (action.thinking || message.thinking) ? "completed" : message.thinkingStatus,
        media: action.media ?? message.media,
        streaming: false,
        timestamp,
        status: String(action.metadata?.status || "") || message.status,
        modelRoute: modelRouteFromMetadata(action.metadata) ?? message.modelRoute,
        ...(durationMs === undefined ? {} : { durationMs }),
      };
    }, true);
    return {
      ...next,
      turnStates: finalOwnsActiveTurn
        ? setTurnState(state, action.session_id, idleTurnState)
        : next.turnStates,
      activeTurnId: action.session_id === state.sessionId
        && finalOwnsActiveTurn ? "" : next.activeTurnId,
      approvalRequests: finalTurnId
        ? removeApprovalRequestsForTurn(state, action.session_id, finalTurnId)
        : state.approvalRequests,
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
    if ((existing.approvalState === "rejected" || existing.approvalState === "cancelled"
      || existing.approvalState === "expired" || existing.approvalState === "unavailable")
      && tool.status === "running") {
      continue;
    }
    if ((existing.approvalState === "rejected" || existing.approvalState === "cancelled"
      || existing.approvalState === "expired" || existing.approvalState === "unavailable")
      && tool.status === "completed") {
      // 审批拒绝/失效后迟到的 completed 不能把安全终态伪装成成功。
      continue;
    }
    // 终态不能因重连快照或迟到 started 事件回退；未知终态也不能被伪装成成功。
    if (isTerminalToolStatus(existing.status) && !isTerminalToolStatus(tool.status)) {
      // 虽然状态保持终态，迟到 started 仍可能携带此前缺失的目标、参数和
      // 开始时间；合并这些展示字段，避免乱序只剩一行“未知工具”。
      merged[index] = mergeToolFields(existing, tool, true);
      continue;
    }
    if (isKnownTerminalToolStatus(existing.status)
      && tool.status === "unknown"
      && !isKnownTerminalToolStatus(tool.status)) {
      merged[index] = mergeToolFields(existing, tool, true);
      continue;
    }
    if (isKnownTerminalToolStatus(existing.status)
      && isKnownTerminalToolStatus(tool.status)
      && existing.status !== tool.status
      && !isLaterToolTerminal(tool, existing)) {
      // 同一 call 的终态事件可能因重连乱序到达；有可靠结束时间时只接收
      // 更新的一条，没有时间戳则保留先到终态，避免重复帧随机改写结果。
      continue;
    }
    merged[index] = mergeToolFields(existing, tool);
  }
  return merged;
}

function mergeToolFields(existing: ToolActivity, incoming: ToolActivity, preserveStatus = false): ToolActivity {
  return {
    ...existing,
    ...incoming,
    ...(preserveStatus ? { status: existing.status } : {}),
    arguments: incoming.arguments === undefined ? existing.arguments : incoming.arguments,
    resultPreview: incoming.resultPreview || existing.resultPreview,
    startedAt: incoming.startedAt || existing.startedAt,
    endedAt: incoming.endedAt || existing.endedAt,
    durationMs: incoming.durationMs ?? existing.durationMs,
    approvalRequestedAt: incoming.approvalRequestedAt || existing.approvalRequestedAt,
    approvalResolvedAt: incoming.approvalResolvedAt || existing.approvalResolvedAt,
    approvalWaitMs: incoming.approvalWaitMs ?? existing.approvalWaitMs,
    executionMs: incoming.executionMs ?? existing.executionMs,
    groupDurationMs: incoming.groupDurationMs ?? existing.groupDurationMs,
    approvalId: incoming.approvalId || existing.approvalId,
    approvalState: incoming.approvalState ?? existing.approvalState,
    resultKind: incoming.resultKind || existing.resultKind,
    isTruncated: incoming.isTruncated ?? existing.isTruncated,
    exitCode: incoming.exitCode ?? existing.exitCode,
    errorCode: incoming.errorCode || existing.errorCode,
  };
}

function removeApprovalRequestsForTurn(state: ChatState, sessionId: string, turnId: string): NonNullable<ChatState["approvalRequests"]> {
  const requests = state.approvalRequests ?? {};
  const next = { ...requests };
  const sessionRequests = { ...(next[sessionId] ?? {}) };
  for (const [approvalId, approval] of Object.entries(sessionRequests)) {
    if (!turnId || approval.turn_id === turnId) delete sessionRequests[approvalId];
  }
  if (Object.keys(sessionRequests).length) next[sessionId] = sessionRequests;
  else delete next[sessionId];
  return next;
}

/** 将服务端 wire 状态映射为有限集合；未知值必须落到 unknown。 */
export function normalizeToolStatus(status: unknown): ToolStatus {
  const value = String(status ?? "").trim().toLowerCase();
  if (value === "running" || value === "in_progress" || value === "pending") return "running";
  if (value === "ok" || value === "completed" || value === "complete" || value === "success" || value === "done") return "completed";
  if (value === "error" || value === "failed" || value === "failure") return "error";
  if (value === "interrupted" || value === "stopped") return "interrupted";
  if (value === "cancelled" || value === "canceled") return "cancelled";
  if (value === "expired" || value === "timeout" || value === "timed_out") return "expired";
  if (value === "unavailable" || value === "disconnected") return "unavailable";
  if (value === "rejected" || value === "denied") return "rejected";
  return "unknown";
}

function isTerminalToolStatus(status: ToolStatus): boolean {
  // unknown 来自 completed/历史快照时也必须按 fail-closed 终态处理，
  // 防止迟到的 approval.requested 把一次已经结束的调用重新打开。
  return status !== "running";
}

function isKnownTerminalToolStatus(status: ToolStatus): boolean {
  return status === "completed"
    || status === "error"
    || status === "interrupted"
    || status === "cancelled"
    || status === "expired"
    || status === "unavailable"
    || status === "rejected";
}

function isLaterToolTerminal(incoming: ToolActivity, existing: ToolActivity): boolean {
  if (!incoming.endedAt || !existing.endedAt) return false;
  const incomingAt = Date.parse(incoming.endedAt);
  const existingAt = Date.parse(existing.endedAt);
  return Number.isFinite(incomingAt) && Number.isFinite(existingAt) && incomingAt >= existingAt;
}

function isTerminalApprovalState(state: ToolActivity["approvalState"]): boolean {
  return state === "rejected" || state === "cancelled" || state === "expired" || state === "unavailable";
}

function normalizeApprovalState(value: unknown): NonNullable<ToolActivity["approvalState"]> {
  const state = String(value ?? "").trim().toLowerCase();
  if (state === "pending" || state === "submitting" || state === "allowed-once" || state === "allowed-session"
    || state === "rejected" || state === "cancelled" || state === "expired" || state === "unavailable") {
    return state;
  }
  return "none";
}

function normalizeApprovalDecision(
  decision: unknown,
  state: ToolActivity["approvalState"],
): ResolvedApproval["decision"] {
  if (decision === "rejected" || decision === "cancelled" || decision === "expired" || decision === "unavailable") {
    return decision;
  }
  if (state === "rejected") {
    return state;
  }
  // cancelled/expired/unavailable 是服务端终态，不是用户的允许/拒绝决定；
  // 保留 null 让诊断层能区分“没有用户决定”和明确 rejected。
  if (state === "cancelled" || state === "expired" || state === "unavailable") return null;
  // 没有可识别的 state/decision 时只记录未知回执，不把它伪装成放行。
  return state === "allowed-once" || state === "allowed-session" ? state : null;
}

function normalizeResolutionState(state: unknown, decision: unknown): NonNullable<ToolActivity["approvalState"]> {
  const fromState = normalizeApprovalState(state);
  const fromDecision = normalizeApprovalState(decision);
  // 旧服务端可能同时携带 state=pending 与明确 decision；合法 decision
  // 优先，避免 UI 在已决定后继续显示等待授权。
  if (decision === "allowed-once" || decision === "allowed-session" || decision === "rejected" || decision === "cancelled"
    || decision === "expired" || decision === "unavailable") {
    return fromDecision;
  }
  // resolved 只能携带终态；pending/submitting 即使来自旧服务端也不能
  // 让前端移除审批卡后继续显示一个没有操作入口的永久等待状态。
  if (fromState === "allowed-once" || fromState === "allowed-session" || fromState === "rejected"
    || fromState === "cancelled" || fromState === "expired" || fromState === "unavailable") {
    return fromState;
  }
  // 缺失或未知的终态不能默认成 allowed-once；否则一帧损坏/未来版本的
  // approval.resolved 可能被 UI 误解为已放行。按不可用收敛，既保持
  // fail-closed，也让等待中的工具有明确终态而不会永久卡在 running。
  return "unavailable";
}

function mergeResolvedApproval(previous: ResolvedApproval | undefined, incoming: ResolvedApproval): ResolvedApproval {
  if (!previous || previous.session_id !== incoming.session_id) return incoming;
  // 终态一旦确认不能被迟到的 allowed-once/未知回执重新打开；若两个回执
  // 都是终态，保留第一次决定并补充后到的关联字段。
  if (isTerminalApprovalState(previous.state)) {
    return {
      ...incoming,
      ...previous,
      turn_id: previous.turn_id || incoming.turn_id,
      call_id: previous.call_id || incoming.call_id,
      requested_at: previous.requested_at || incoming.requested_at,
      decided_at: previous.decided_at || incoming.decided_at,
      error_code: previous.error_code || incoming.error_code,
    };
  }
  return {
    ...previous,
    ...incoming,
    turn_id: incoming.turn_id || previous.turn_id,
    call_id: incoming.call_id || previous.call_id,
    requested_at: incoming.requested_at || previous.requested_at,
    decided_at: incoming.decided_at || previous.decided_at,
    error_code: incoming.error_code || previous.error_code,
  };
}

function rememberResolvedApproval(
  current: ChatState["resolvedApprovals"],
  resolution: ResolvedApproval,
): NonNullable<ChatState["resolvedApprovals"]> {
  const next = {
    ...(current ?? {}),
    [resolution.session_id]: {
      ...(current?.[resolution.session_id] ?? {}),
      [resolution.id]: resolution,
    },
  };
  const entries = Object.entries(next[resolution.session_id]);
  if (entries.length <= 256) return next;
  // 只保留短期乱序窗口，避免长时间运行的会话在浏览器内无限增长。
  entries.sort(([, left], [, right]) => {
    const leftTime = Date.parse(left.decided_at || "");
    const rightTime = Date.parse(right.decided_at || "");
    return (Number.isFinite(leftTime) ? leftTime : 0) - (Number.isFinite(rightTime) ? rightTime : 0);
  });
  for (const [id] of entries.slice(0, entries.length - 256)) delete next[resolution.session_id][id];
  return next;
}

function findPendingApproval(
  state: ChatState,
  sessionId: string,
  turnId: string,
  callId: string,
  approvalId?: string,
): ApprovalRequest | undefined {
  const requests = Object.values(state.approvalRequests?.[sessionId] ?? {});
  if (approvalId) {
    const direct = state.approvalRequests?.[sessionId]?.[approvalId];
    if (direct) return direct;
  }
  return requests.find((approval) => (
    approval.session_id === sessionId
    && approval.turn_id === turnId
    && approval.call_id === callId
  ));
}

function findResolvedApproval(
  state: ChatState,
  sessionId: string,
  turnId: string,
  callId: string,
  approvalId?: string,
): ResolvedApproval | undefined {
  const resolutions = Object.values(state.resolvedApprovals?.[sessionId] ?? {});
  if (approvalId) {
    const direct = state.resolvedApprovals?.[sessionId]?.[approvalId];
    if (direct) return direct;
  }
  return resolutions.find((resolution) => (
    resolution.session_id === sessionId
    && (!!turnId && resolution.turn_id === turnId)
    && (!!callId && resolution.call_id === callId)
  ));
}

function approvalTerminalStatus(state: ToolActivity["approvalState"]): ToolStatus | undefined {
  if (state === "rejected" || state === "cancelled" || state === "expired" || state === "unavailable") return state;
  return undefined;
}

function applyResolvedApproval(
  tool: ToolActivity,
  resolution: ResolvedApproval,
  options: { priorToToolEvent?: boolean } = {},
): ToolActivity {
  const terminalStatus = approvalTerminalStatus(resolution.state);
  const resolvedAt = resolution.decided_at;
  const toolEndedAt = tool.endedAt ? Date.parse(tool.endedAt) : NaN;
  const approvalResolvedAt = resolvedAt ? Date.parse(resolvedAt) : NaN;
  // 已完成工具缺少任一端时间时无法证明审批先于工具终态；保守保留
  // completed，避免旧协议的迟到 rejected 把成功结果回退成失败。
  const resolvedBeforeToolEnd = Number.isFinite(toolEndedAt)
    && Number.isFinite(approvalResolvedAt)
    && approvalResolvedAt <= toolEndedAt;
  const canSetApprovalTerminal = tool.status === "running" || tool.status === "unknown"
    || (Boolean(terminalStatus) && tool.status === "completed"
      && (resolvedBeforeToolEnd || options.priorToToolEvent === true));
  const changesToApprovalTerminal = Boolean(terminalStatus && canSetApprovalTerminal);
  const requestedAt = tool.approvalRequestedAt || resolution.requested_at;
  const approvalWaitMs = tool.approvalWaitMs ?? elapsedDurationMs(requestedAt, resolvedAt);
  return {
    ...tool,
    status: terminalStatus && canSetApprovalTerminal ? terminalStatus : tool.status,
    approvalId: resolution.id,
    approvalState: resolution.state,
    ...(changesToApprovalTerminal ? { resultPreview: "" } : {}),
    ...(requestedAt ? { approvalRequestedAt: requestedAt } : {}),
    ...(resolvedAt ? { approvalResolvedAt: resolvedAt } : {}),
    ...(approvalWaitMs === undefined ? {} : { approvalWaitMs }),
    ...(resolution.error_code ? { errorCode: resolution.error_code } : {}),
  };
}

function decorateToolWithApproval(
  state: ChatState,
  sessionId: string,
  turnId: string,
  tool: ToolActivity,
): ToolActivity {
  const pending = findPendingApproval(state, sessionId, turnId, tool.callId, tool.approvalId);
  const resolved = findResolvedApproval(state, sessionId, turnId, tool.callId, tool.approvalId);
  if (resolved) return applyResolvedApproval(tool, resolved, { priorToToolEvent: true });
  if (!pending) return tool;
  const requestedAt = pending.requested_at || pending.created_at;
  if (isKnownTerminalToolStatus(tool.status)) {
    // 终态工具不能因迟到的审批请求重新进入 pending；保留关联时间供详情展示，
    // 但等待卡由服务端 resolved/历史状态决定。
    return {
      ...tool,
      approvalId: pending.id,
      ...(requestedAt ? { approvalRequestedAt: requestedAt } : {}),
    };
  }
  return {
    ...tool,
    approvalId: pending.id,
    approvalState: "pending",
    ...(requestedAt ? { approvalRequestedAt: requestedAt } : {}),
  };
}

function isTerminalMessageStatus(status: string | undefined): boolean {
  return status === "completed" || status === "ok" || status === "error"
    || status === "interrupted" || status === "cancelled" || status === "expired"
    || status === "unavailable" || status === "rejected";
}

function isClosedMessage(message: ChatMessage): boolean {
  return message.streaming === false || isTerminalMessageStatus(message.status);
}

function interruptTool(tool: ToolActivity, endedAt: string, terminalStatus: "interrupted" | "cancelled" | "expired"): ToolActivity {
  const waiting = tool.approvalState === "pending" || tool.approvalState === "submitting";
  // unknown 没有可靠的终态语义；Turn 中断时按未完成工具收敛，避免
  // 重连后长期停在“状态未知”且没有结束时间。
  if (tool.status !== "running" && tool.status !== "unknown" && !waiting) return tool;
  const durationMs = tool.durationMs ?? elapsedDurationMs(tool.startedAt, endedAt);
  return {
    ...tool,
    status: terminalStatus,
    approvalState: waiting ? "cancelled" : tool.approvalState,
    endedAt: tool.endedAt || endedAt,
    errorCode: tool.errorCode || "turn_interrupted",
    ...(durationMs === undefined ? {} : { durationMs }),
  };
}

function normalizeOptionalNumber(value: unknown): number | undefined {
  const number = typeof value === "number" ? value : typeof value === "string" && value.trim() ? Number(value) : NaN;
  return Number.isFinite(number) ? number : undefined;
}

function normalizeOptionalDuration(value: unknown): number | undefined {
  const duration = normalizeOptionalNumber(value);
  return duration !== undefined && duration >= 0 ? duration : undefined;
}

function updateSessionTurn(
  state: ChatState,
  sessionId: string,
  turnId: string,
  updater: (message: ChatMessage) => ChatMessage,
  allowMissingBackground = false,
): ChatState {
  if (!sessionId || !turnId) return state;
  const source = getSessionMessages(state, sessionId);
  let index = source.findIndex((message) => message.role === "assistant" && message.turnId === turnId);
  const messages = [...source];
  if (index < 0) {
    const knownTurn = state.turnStates[sessionId]?.turnId === turnId
      || source.some((message) => message.turnId === turnId)
      || Object.values(state.approvalRequests?.[sessionId] ?? {}).some((approval) => (
        approval.session_id === sessionId && approval.turn_id === turnId
      ))
      || Object.values(state.resolvedApprovals?.[sessionId] ?? {}).some((approval) => (
        approval.turn_id === turnId
      ));
    if (sessionId !== state.sessionId && !knownTurn && !allowMissingBackground) return state;
    // WebSocket 帧可能乱序或重连后只收到增量事件；先创建可合并的草稿，
    // 后续 turn.started/turn.snapshot 会复用它而不是丢弃工具生命周期。
    index = messages.length;
    messages.push(createDraft(turnId));
  }
  const updated = updater(messages[index]);
  if (updated === messages[index]) return state;
  messages[index] = updated;
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
      durationMs: durationFromRow(row),
      presentation: row.proactive ? undefined : readPresentation(row.metadata?.presentation),
      proactive: Boolean(row.proactive),
      source: row.proactive
        ? (String(row.metadata?.source || "proactive_conversation") as ChatMessage["source"])
        : undefined,
      modelRoute: modelRouteFromMetadata(row.metadata),
    };
  });
}

function modelRouteFromMetadata(metadata: Record<string, unknown> | undefined): ChatMessage["modelRoute"] {
  const route = metadata?.model_route;
  if (!route || typeof route !== "object" || Array.isArray(route)) return undefined;
  const value = route as Record<string, unknown>;
  const connectionId = String(value.connection_id || "");
  const modelId = String(value.model_id || "");
  const adapter = String(value.adapter || "");
  if (!connectionId || !modelId) return undefined;
  return {
    connection_id: connectionId,
    model_id: modelId,
    connection_name: String(value.connection_name || "") || undefined,
    model_display_name: String(value.model_display_name || "") || undefined,
    ...(adapter ? { adapter: adapter as ModelAdapterId } : {}),
  };
}

function durationFromRow(row: MessageRow): number | undefined {
  return normalizeDuration(row.duration_ms)
    ?? normalizeDuration(row.elapsed_ms)
    ?? durationFromMetadata(row.metadata);
}

function durationFromMetadata(metadata: Record<string, unknown> | undefined): number | undefined {
  if (!metadata) return undefined;
  return normalizeDuration(metadata.duration_ms)
    ?? normalizeDuration(metadata.elapsed_ms)
    ?? normalizeDuration(metadata.durationMs);
}

function normalizeDuration(value: unknown): number | undefined {
  const duration = typeof value === "number" ? value : typeof value === "string" ? Number(value) : NaN;
  return Number.isFinite(duration) && duration >= 0 ? duration : undefined;
}

function elapsedDurationMs(start: string | undefined, end: string | undefined): number | undefined {
  if (!start || !end) return undefined;
  const startTime = Date.parse(start);
  const endTime = Date.parse(end);
  if (Number.isNaN(startTime) || Number.isNaN(endTime)) return undefined;
  return Math.max(0, endTime - startTime);
}

function isRuntimeMessage(message: ChatMessage | undefined): boolean {
  return Boolean(message && (message.seq === undefined || message.seq < 0));
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
    status: normalizeToolStatus(call.status),
    arguments: call.arguments,
    resultPreview: String(call.result_preview ?? call.result ?? ""),
    ...(call.approval_id ? { approvalId: call.approval_id } : {}),
    ...(call.approval_state ? { approvalState: normalizeApprovalState(call.approval_state) } : {}),
    ...(call.started_at ? { startedAt: call.started_at } : {}),
    ...(call.ended_at ? { endedAt: call.ended_at } : {}),
    ...(normalizeOptionalDuration(call.duration_ms) !== undefined ? { durationMs: normalizeOptionalDuration(call.duration_ms) } : {}),
    ...(call.approval_requested_at ? { approvalRequestedAt: call.approval_requested_at } : {}),
    ...(call.approval_resolved_at ? { approvalResolvedAt: call.approval_resolved_at } : {}),
    ...(normalizeOptionalDuration(call.approval_wait_ms) !== undefined ? { approvalWaitMs: normalizeOptionalDuration(call.approval_wait_ms) } : {}),
    ...(normalizeOptionalDuration(call.execution_ms) !== undefined ? { executionMs: normalizeOptionalDuration(call.execution_ms) } : {}),
    ...(normalizeOptionalDuration(call.group_duration_ms) !== undefined ? { groupDurationMs: normalizeOptionalDuration(call.group_duration_ms) } : {}),
    ...(call.result_kind ? { resultKind: call.result_kind } : {}),
    ...(call.is_truncated === undefined || call.is_truncated === null ? {} : { isTruncated: Boolean(call.is_truncated) }),
    ...(normalizeOptionalNumber(call.exit_code) !== undefined ? { exitCode: normalizeOptionalNumber(call.exit_code) } : {}),
    ...(call.error_code ? { errorCode: call.error_code } : {}),
  })));
}

function updateToolApproval(
  state: ChatState,
  sessionId: string,
  matcher: { turnId?: string; callId?: string; approvalId?: string },
  updater: (tool: ToolActivity) => ToolActivity,
): ChatState {
  const source = getSessionMessages(state, sessionId);
  let changed = false;
  const messages = source.map((message) => {
    if (message.role !== "assistant") return message;
    if (matcher.turnId && message.turnId !== matcher.turnId) return message;
    let messageChanged = false;
    const tools = message.tools.map((tool) => {
      const matches = (matcher.callId && tool.callId === matcher.callId)
        || (matcher.approvalId && tool.approvalId === matcher.approvalId);
      if (!matches) return tool;
      changed = true;
      messageChanged = true;
      return updater(tool);
    });
    return messageChanged ? { ...message, tools } : message;
  });
  if (!changed) return state;
  return {
    ...state,
    messages: sessionId === state.sessionId ? messages : state.messages,
    sessionMessages: setSessionMessages(state, sessionId, messages),
  };
}
