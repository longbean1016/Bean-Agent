import { describe, expect, it } from "vitest";

import { initialChatState, mergeTimeline, notificationRowsToMessages, reduceChatFrame, rowsToMessages } from "./chatReducer";

describe("reduceChatFrame", () => {
  it("maps interrupted display snapshots without exposing placeholder content", () => {
    const [message] = rowsToMessages([{
      id: "web:one:1", seq: 1, role: "assistant", content: "[用户已停止生成]",
      status: "interrupted", turn_id: "turn-1", reasoning_content: "",
      interrupted_display_content: "partial reply",
      interrupted_display_reasoning: "partial thinking",
      tool_chain: [{ calls: [
        { call_id: "done", name: "shell", status: "ok", result: "done" },
        { call_id: "stopped", name: "search", status: "interrupted", result: "partial" },
      ] }],
    }]);

    expect(message).toMatchObject({
      content: "partial reply", thinking: "partial thinking", status: "interrupted",
      thinkingStatus: "interrupted",
      tools: [{ status: "completed" }, { status: "interrupted" }],
    });
  });

  it("restores every interrupted thinking message as stopped", () => {
    const [message] = rowsToMessages([{
      id: "web:one:1", seq: 1, role: "assistant", content: "[用户已停止生成]",
      status: "interrupted", turn_id: "turn-1", reasoning_content: "",
      interrupted_display_content: "partial reply",
      interrupted_display_reasoning: "partial thinking",
      interrupted_thinking_status: "completed",
      tool_chain: [],
    }]);

    expect(message).toMatchObject({
      content: "partial reply",
      thinking: "partial thinking",
      thinkingStatus: "interrupted",
    });
  });

  it("keeps thinking active until message.final completes the turn", () => {
    let state = reduceChatFrame({ ...initialChatState, sessionId: "web:one" }, {
      type: "turn.started", request_id: "r1", session_id: "web:one", turn_id: "turn-1",
    });
    state = reduceChatFrame(state, {
      type: "react.thinking.delta", session_id: "web:one", turn_id: "turn-1", delta: "先查询资料",
    });
    expect(state.messages[0]).toMatchObject({ thinkingStatus: "running" });

    state = reduceChatFrame(state, {
      type: "react.tool.started", session_id: "web:one", turn_id: "turn-1",
      call_id: "call-1", tool_name: "search", arguments: {},
    });
    expect(state.messages[0]).toMatchObject({ thinkingStatus: "running" });

    state = reduceChatFrame(state, {
      type: "react.tool.completed", session_id: "web:one", turn_id: "turn-1",
      call_id: "call-1", tool_name: "search", status: "ok", result_preview: "result",
    });
    expect(state.messages[0]).toMatchObject({ thinkingStatus: "running" });

    state = reduceChatFrame(state, {
      type: "answer.delta", session_id: "web:one", turn_id: "turn-1", delta: "最终回答",
    });
    expect(state.messages[0]).toMatchObject({ thinkingStatus: "running" });

    state = reduceChatFrame(state, {
      type: "message.final", request_id: "r1", session_id: "web:one", turn_id: "turn-1",
      content: "最终回答", thinking: "先查询资料", metadata: { status: "ok" },
    });
    expect(state.messages[0]).toMatchObject({ thinkingStatus: "completed" });
  });

  it("marks thinking as stopped whenever the turn is interrupted", () => {
    let beforeAnswer = reduceChatFrame({ ...initialChatState, sessionId: "web:one" }, {
      type: "turn.started", request_id: "r1", session_id: "web:one", turn_id: "turn-1",
    });
    beforeAnswer = reduceChatFrame(beforeAnswer, {
      type: "react.thinking.delta", session_id: "web:one", turn_id: "turn-1", delta: "正在思考",
    });
    beforeAnswer = reduceChatFrame(beforeAnswer, {
      type: "turn.interrupted", request_id: "stop-1", session_id: "web:one", turn_id: "turn-1", status: "interrupted",
    });
    expect(beforeAnswer.messages[0]).toMatchObject({ thinkingStatus: "interrupted" });

    let afterAnswer = reduceChatFrame({ ...initialChatState, sessionId: "web:one" }, {
      type: "turn.started", request_id: "r2", session_id: "web:one", turn_id: "turn-2",
    });
    afterAnswer = reduceChatFrame(afterAnswer, {
      type: "react.thinking.delta", session_id: "web:one", turn_id: "turn-2", delta: "已经想好",
    });
    afterAnswer = reduceChatFrame(afterAnswer, {
      type: "answer.delta", session_id: "web:one", turn_id: "turn-2", delta: "部分正文",
    });
    afterAnswer = reduceChatFrame(afterAnswer, {
      type: "turn.interrupted", request_id: "stop-2", session_id: "web:one", turn_id: "turn-2", status: "interrupted",
    });
    expect(afterAnswer.messages[0]).toMatchObject({ thinkingStatus: "interrupted" });
  });

  it("keeps a running snapshot in thinking state even when it already has content", () => {
    const state = reduceChatFrame({ ...initialChatState, sessionId: "web:one" }, {
      type: "turn.snapshot", request_id: "r1", session_id: "web:one", turn_id: "turn-1",
      user_message: "question", user_media: [], content: "partial answer", thinking: "partial thinking", tools: [],
      status: "running",
    });

    expect(state.messages.find((message) => message.role === "assistant")).toMatchObject({
      content: "partial answer",
      thinkingStatus: "running",
      streaming: true,
    });
  });

  it("treats an interrupted tool as unfinished thinking even after transitional content", () => {
    let state = reduceChatFrame({ ...initialChatState, sessionId: "web:one" }, {
      type: "turn.started", request_id: "r1", session_id: "web:one", turn_id: "turn-1",
    });
    state = reduceChatFrame(state, {
      type: "react.thinking.delta", session_id: "web:one", turn_id: "turn-1", delta: "准备查询",
    });
    state = reduceChatFrame(state, {
      type: "answer.delta", session_id: "web:one", turn_id: "turn-1", delta: "我先查一下",
    });
    state = reduceChatFrame(state, {
      type: "react.tool.started", session_id: "web:one", turn_id: "turn-1",
      call_id: "call-1", tool_name: "shell", arguments: {},
    });
    expect(state.messages[0]).toMatchObject({ thinkingStatus: "running" });
    state = reduceChatFrame(state, {
      type: "turn.interrupted", request_id: "stop-1", session_id: "web:one", turn_id: "turn-1", status: "interrupted",
    });

    expect(state.messages[0]).toMatchObject({
      content: "我先查一下",
      thinkingStatus: "interrupted",
      tools: [{ callId: "call-1", status: "running" }],
    });
  });

  it("renders the next turn after a completed turn", () => {
    let state = reduceChatFrame({ ...initialChatState, sessionId: "web:one" }, {
      type: "turn.started", request_id: "r1", session_id: "web:one", turn_id: "turn-1",
    });
    state = reduceChatFrame(state, {
      type: "message.final", request_id: "r1", session_id: "web:one", turn_id: "turn-1", content: "first answer",
    });
    state = reduceChatFrame(state, {
      type: "ui.user.append",
      message: { id: "user-r2", role: "user", content: "second", thinking: "", media: [], tools: [] },
    });
    state = reduceChatFrame(state, { type: "ui.turn.submitted", sessionId: "web:one", requestId: "r2" });
    state = reduceChatFrame(state, {
      type: "turn.started", request_id: "r2", session_id: "web:one", turn_id: "turn-2",
    });
    state = reduceChatFrame(state, {
      type: "answer.delta", session_id: "web:one", turn_id: "turn-2", delta: "second answer",
    });

    expect(state.turnStates["web:one"].status).toBe("running");
    expect(state.messages.find((message) => message.turnId === "turn-2" && message.role === "assistant"))
      .toMatchObject({ content: "second answer", streaming: true });
  });
  it("提交确认后会从提交中切换到排队状态", () => {
    let state = { ...initialChatState, sessionId: "web:one" };
    state = reduceChatFrame(state, {
      type: "ui.turn.submitted", sessionId: "web:one", requestId: "r1",
    });
    state = reduceChatFrame(state, {
      type: "turn.queued", session_id: "web:one", request_id: "r1", position: 1,
    });
    expect(state.turnStates["web:one"].status).toBe("queued");
  });

  it("压缩上下文期间保留当前 Turn，并在完成后恢复运行态", () => {
    let state = reduceChatFrame({ ...initialChatState, sessionId: "web:one" }, {
      type: "turn.started", request_id: "r1", session_id: "web:one", turn_id: "turn-1",
    });
    state = reduceChatFrame(state, {
      type: "context.compaction.started", session_id: "web:one", turn_id: "turn-1",
      trigger: "soft_limit", estimated_tokens: 800,
    });
    expect(state.turnStates["web:one"]).toMatchObject({ status: "compacting", turnId: "turn-1" });
    expect(state.activeTurnId).toBe("turn-1");

    state = reduceChatFrame(state, {
      type: "context.compaction.completed", session_id: "web:one", turn_id: "turn-1",
      trigger: "soft_limit", estimated_tokens: 800, compacted: true,
    });
    expect(state.turnStates["web:one"]).toMatchObject({ status: "running", turnId: "turn-1" });
  });

  it("按会话保存完整上下文用量并拒绝旧 Turn 的迟到估算", () => {
    let state = reduceChatFrame({ ...initialChatState, sessionId: "web:one" }, {
      type: "turn.started", request_id: "r1", session_id: "web:one", turn_id: "turn-1",
    });
    state = reduceChatFrame(state, {
      type: "context.usage.updated", session_id: "web:one", turn_id: "turn-1",
      used_tokens: 65500, context_window: 1000000, soft_limit_tokens: 740000,
      hard_input_tokens: 991808, context_window_source: "provider_catalog", estimate_source: "heuristic",
      breakdown: { system_prompt_tokens: 1600, tools_tokens: 6900, conversation_tokens: 49700, overhead_tokens: 7300 },
      sections: [{ name: "identity", estimated_tokens: 120, static: true, cache_hit: true }],
    });
    expect(state.contextUsage["web:one"]).toMatchObject({
      usedTokens: 65500, contextWindow: 1000000, contextWindowSource: "provider_catalog",
    });

    const unchanged = reduceChatFrame(state, {
      type: "context.usage.updated", session_id: "web:one", turn_id: "turn-old",
      used_tokens: 1, context_window: 2, soft_limit_tokens: 1, hard_input_tokens: 1,
      context_window_source: "unknown", estimate_source: "heuristic",
      breakdown: {}, sections: [],
    });
    expect(unchanged).toBe(state);
  });

  it("压缩失败会清理当前 Turn 状态", () => {
    let state = reduceChatFrame({ ...initialChatState, sessionId: "web:one" }, {
      type: "turn.started", request_id: "r1", session_id: "web:one", turn_id: "turn-1",
    });
    state = reduceChatFrame(state, {
      type: "context.compaction.started", session_id: "web:one", turn_id: "turn-1",
      trigger: "context_overflow", estimated_tokens: 1000,
    });
    state = reduceChatFrame(state, {
      type: "context.compaction.failed", session_id: "web:one", turn_id: "turn-1",
      trigger: "context_overflow", estimated_tokens: 1000, message: "压缩失败",
    });

    expect(state.turnStates["web:one"]).toMatchObject({ status: "idle", turnId: "" });
    expect(state.activeTurnId).toBe("");
  });

  it("重连快照不会覆盖同一 Turn 的压缩状态", () => {
    let state = reduceChatFrame({ ...initialChatState, sessionId: "web:one" }, {
      type: "turn.started", request_id: "r1", session_id: "web:one", turn_id: "turn-1",
    });
    state = reduceChatFrame(state, {
      type: "context.compaction.started", session_id: "web:one", turn_id: "turn-1",
      trigger: "soft_limit", estimated_tokens: 800,
    });
    state = reduceChatFrame(state, {
      type: "turn.snapshot", session_id: "web:one", turn_id: "turn-1", request_id: "r1",
      user_message: "当前问题", user_media: [], content: "", thinking: "", tools: [], status: "running",
    });

    expect(state.turnStates["web:one"].status).toBe("compacting");
  });

  it("排队会话切走再切回仍保留用户问题", () => {
    let state = reduceChatFrame({ ...initialChatState, sessionId: "web:one" }, {
      type: "ui.user.append",
      message: {
        id: "user-r1", role: "user", content: "等待执行的问题", thinking: "", media: [], tools: [],
      },
    });
    state = reduceChatFrame(state, {
      type: "turn.queued", request_id: "r1", session_id: "web:one", position: 1,
    });
    state = reduceChatFrame(state, {
      type: "ui.session.select", sessionId: "web:two", messages: [],
    });
    state = reduceChatFrame(state, {
      type: "ui.session.select", sessionId: "web:one", messages: [],
    });

    expect(state.messages).toHaveLength(1);
    expect(state.messages[0]).toMatchObject({ role: "user", content: "等待执行的问题" });
    expect(state.turnStates["web:one"].status).toBe("queued");
  });

  it("后台会话的流式文本思考和工具状态在切回后完整恢复", () => {
    let state = reduceChatFrame({ ...initialChatState, sessionId: "web:one" }, {
      type: "ui.user.append",
      message: {
        id: "user-r1", role: "user", content: "分析项目", thinking: "", media: [], tools: [],
      },
    });
    state = reduceChatFrame(state, {
      type: "ui.session.select", sessionId: "web:two", messages: [],
    });
    state = reduceChatFrame(state, {
      type: "turn.started", request_id: "r1", session_id: "web:one", turn_id: "turn-1",
    });
    state = reduceChatFrame(state, {
      type: "react.thinking.delta", session_id: "web:one", turn_id: "turn-1", delta: "正在检查",
    });
    state = reduceChatFrame(state, {
      type: "answer.delta", session_id: "web:one", turn_id: "turn-1", delta: "阶段结果",
    });
    state = reduceChatFrame(state, {
      type: "react.tool.started", session_id: "web:one", turn_id: "turn-1",
      call_id: "call-1", tool_name: "list_dir", arguments: { path: "." },
    });
    state = reduceChatFrame(state, {
      type: "react.tool.completed", session_id: "web:one", turn_id: "turn-1",
      call_id: "call-1", tool_name: "list_dir", status: "ok", result_preview: "agent, tests",
    });
    state = reduceChatFrame(state, {
      type: "ui.session.select", sessionId: "web:one", messages: [],
    });

    expect(state.messages).toHaveLength(2);
    expect(state.messages[0]).toMatchObject({ role: "user", content: "分析项目" });
    expect(state.messages[1]).toMatchObject({
      turnId: "turn-1",
      content: "阶段结果",
      thinking: "正在检查",
      tools: [{ callId: "call-1", status: "completed", resultPreview: "agent, tests" }],
    });
  });

  it("turn snapshot 在刷新后重建用户消息和流式草稿", () => {
    let state = reduceChatFrame({ ...initialChatState, sessionId: "web:one" }, {
      type: "ui.session.select",
      sessionId: "web:one",
      messages: [],
    });

    state = reduceChatFrame(state, {
      type: "turn.snapshot",
      session_id: "web:one",
      turn_id: "turn-1",
      request_id: "r1",
      user_message: "读取项目",
      user_media: ["D:/tmp/a.png"],
      content: "已经读到",
      thinking: "正在分析",
      tools: [{
        call_id: "call-1",
        name: "read_file",
        status: "completed",
        arguments: { path: "README.md" },
        result_preview: "project docs",
      }],
      status: "running",
    });
    state = reduceChatFrame(state, {
      type: "answer.delta",
      session_id: "web:one",
      turn_id: "turn-1",
      delta: "更多",
    });

    expect(state.activeTurnId).toBe("turn-1");
    expect(state.turnStates["web:one"]).toMatchObject({ status: "running", turnId: "turn-1", requestId: "r1" });
    expect(state.messages).toHaveLength(2);
    expect(state.messages[0]).toMatchObject({
      id: "user-r1",
      role: "user",
      content: "读取项目",
      media: ["D:/tmp/a.png"],
      turnId: "turn-1",
    });
    expect(state.messages[1]).toMatchObject({
      id: "turn-1",
      role: "assistant",
      content: "已经读到更多",
      thinking: "正在分析",
      streaming: true,
      tools: [{ callId: "call-1", status: "completed", resultPreview: "project docs" }],
    });
  });

  it("turn snapshot 会复用已有乐观用户消息", () => {
    let state = reduceChatFrame({ ...initialChatState, sessionId: "web:one" }, {
      type: "ui.user.append",
      message: {
        id: "user-r1",
        role: "user",
        content: "读取项目",
        thinking: "",
        media: [],
        tools: [],
      },
    });

    state = reduceChatFrame(state, {
      type: "turn.snapshot",
      session_id: "web:one",
      turn_id: "turn-1",
      request_id: "r1",
      user_message: "读取项目",
      user_media: [],
      content: "已经读到",
      thinking: "",
      tools: [],
      status: "running",
    });

    expect(state.messages.filter((message) => message.role === "user")).toHaveLength(1);
    expect(state.messages[0]).toMatchObject({ id: "user-r1", turnId: "turn-1" });
  });

  it("turn snapshot 不会把已完成工具降级为运行中", () => {
    let state = reduceChatFrame(initialChatState, {
      type: "turn.started",
      request_id: "r1",
      session_id: "web:one",
      turn_id: "turn-1",
    });
    state = reduceChatFrame(state, {
      type: "react.tool.started",
      session_id: "web:one",
      turn_id: "turn-1",
      call_id: "call-1",
      tool_name: "read_file",
      arguments: { path: "README.md" },
    });
    state = reduceChatFrame(state, {
      type: "react.tool.completed",
      session_id: "web:one",
      turn_id: "turn-1",
      call_id: "call-1",
      tool_name: "read_file",
      status: "ok",
      result_preview: "done",
    });

    state = reduceChatFrame(state, {
      type: "turn.snapshot",
      session_id: "web:one",
      turn_id: "turn-1",
      request_id: "r1",
      user_message: "读取",
      user_media: [],
      content: "",
      thinking: "",
      tools: [{
        call_id: "call-1",
        name: "read_file",
        status: "running",
        arguments: { path: "README.md" },
        result_preview: "",
      }],
      status: "running",
    });

    const assistant = state.messages.find((message) => message.role === "assistant");
    expect(assistant?.tools[0]).toMatchObject({
      callId: "call-1",
      status: "completed",
      resultPreview: "done",
    });
  });

  it("按 session 保存排队位置并在切换会话后保留", () => {
    const current = { ...initialChatState, sessionId: "web:current" };
    const queued = reduceChatFrame(current, {
      type: "turn.queued",
      request_id: "r-other",
      session_id: "web:other",
      position: 2,
    });
    const selected = reduceChatFrame(queued, {
      type: "ui.session.select",
      sessionId: "web:other",
      messages: [],
    });

    expect(selected.turnStates["web:other"]).toEqual({
      status: "queued",
      queuePosition: 2,
      turnId: "",
      requestId: "r-other",
    });
  });

  it("非当前会话开始和结束时也更新运行状态", () => {
    const current = { ...initialChatState, sessionId: "web:current" };
    const running = reduceChatFrame(current, {
      type: "turn.started",
      request_id: "r-other",
      session_id: "web:other",
      turn_id: "turn-other",
    });
    const finished = reduceChatFrame(running, {
      type: "message.final",
      request_id: "r-other",
      session_id: "web:other",
      turn_id: "turn-other",
      content: "完成",
    });

    expect(running.turnStates["web:other"].status).toBe("running");
    expect(finished.turnStates["web:other"].status).toBe("idle");
    expect(finished.messages).toEqual([]);
  });

  it("队列拒绝会清理乐观消息和会话忙碌状态", () => {
    const current = {
      ...initialChatState,
      sessionId: "web:one",
      messages: [{
        id: "user-r-full", role: "user" as const, content: "问题", thinking: "", media: [], tools: [],
      }],
      turnStates: {
        "web:one": { status: "queued" as const, queuePosition: 1, turnId: "", requestId: "r-full" },
      },
    };

    const rejected = reduceChatFrame(current, {
      type: "error",
      request_id: "r-full",
      session_id: "web:one",
      code: "queue_full",
      message: "当前任务较多，请稍后再试",
    });

    expect(rejected.turnStates["web:one"].status).toBe("idle");
    expect(rejected.messages).toEqual([]);
    expect(rejected.error).toBe("当前任务较多，请稍后再试");
  });

  it("取消排队不会把用户消息标记为中断回答", () => {
    const current = {
      ...initialChatState,
      sessionId: "web:one",
      messages: [{
        id: "user-r1", role: "user" as const, content: "等待中的问题", thinking: "", media: [], tools: [],
      }],
      turnStates: {
        "web:one": { status: "queued" as const, queuePosition: 1, turnId: "", requestId: "r1" },
      },
    };

    const cancelled = reduceChatFrame(current, {
      type: "turn.interrupted",
      request_id: "stop-1",
      session_id: "web:one",
      status: "cancelled",
    });

    expect(cancelled.messages[0].status).toBeUndefined();
    expect(cancelled.turnStates["web:one"].status).toBe("idle");
  });

  it("用 message.final 覆盖同一 turn 的流式草稿", () => {
    let state = reduceChatFrame(initialChatState, {
      type: "turn.started",
      request_id: "r1",
      session_id: "web:one",
      turn_id: "turn-1",
    });
    state = reduceChatFrame(state, {
      type: "answer.delta",
      session_id: "web:one",
      turn_id: "turn-1",
      delta: "流式草稿",
    });
    state = reduceChatFrame(state, {
      type: "message.final",
      request_id: "r1",
      session_id: "web:one",
      turn_id: "turn-1",
      content: "最终内容",
      thinking: "",
      media: [],
    });

    expect(state.messages).toHaveLength(1);
    expect(state.messages[0]).toMatchObject({
      turnId: "turn-1",
      content: "最终内容",
      streaming: false,
    });
  });

  it("按 call_id 更新工具完成状态", () => {
    let state = reduceChatFrame(initialChatState, {
      type: "turn.started",
      request_id: "r1",
      session_id: "web:one",
      turn_id: "turn-1",
    });
    state = reduceChatFrame(state, {
      type: "react.tool.started",
      session_id: "web:one",
      turn_id: "turn-1",
      call_id: "call-1",
      tool_name: "list_dir",
      arguments: { path: "." },
    });
    state = reduceChatFrame(state, {
      type: "react.tool.completed",
      session_id: "web:one",
      turn_id: "turn-1",
      call_id: "call-1",
      tool_name: "list_dir",
      status: "ok",
      result_preview: "agent, tests",
    });

    expect(state.messages[0].tools[0]).toMatchObject({
      callId: "call-1",
      status: "completed",
      resultPreview: "agent, tests",
    });
  });

  it("忽略不属于当前会话的 turn 帧", () => {
    const current = { ...initialChatState, sessionId: "web:current" };
    const next = reduceChatFrame(current, {
      type: "answer.delta",
      session_id: "web:other",
      turn_id: "turn-other",
      delta: "不应出现",
    });

    expect(next).toEqual(current);
  });

  it("主动 final 没有 turn.started 时直接追加并按 message_id 去重", () => {
    const current = { ...initialChatState, sessionId: "web:one" };
    const frame = {
      type: "message.final" as const,
      session_id: "web:one",
      turn_id: "",
      message_id: "message-1",
      content: "顺着上次没做完的部分，我补充一点。",
      metadata: { proactive: true, message_id: "message-1" },
    };

    const once = reduceChatFrame(current, frame);
    const twice = reduceChatFrame(once, frame);

    expect(twice.messages).toHaveLength(1);
    expect(twice.messages[0]).toMatchObject({ id: "message-1", proactive: true, streaming: false });
  });

  it("把独立提醒按生成时间合并进会话展示层", () => {
    const notifications = notificationRowsToMessages([{
      id: "notice-1",
      content: "起来活动一下",
      source: "scheduled_reminder",
      source_id: "job-1",
      scheduled_at: "2026-07-20T09:00:00+08:00",
      generated_at: "2026-07-20T09:00:01+08:00",
      status: "delivered",
      recurring: false,
    }]);
    const timeline = mergeTimeline([{
      id: "user-1", role: "user", content: "早上好", thinking: "", media: [], tools: [],
      timestamp: "2026-07-20T08:00:00+08:00",
    }], notifications);

    expect(timeline.map((message) => message.id)).toEqual(["user-1", "notice-1"]);
    expect(timeline[1]).toMatchObject({ source: "scheduled_reminder", scheduledAt: "2026-07-20T09:00:00+08:00" });
  });

  it("turn.started 将乐观用户消息绑定到真实 turn", () => {
    let state = reduceChatFrame({ ...initialChatState, sessionId: "web:one" }, {
      type: "ui.user.append",
      message: {
        id: "user-r1", role: "user", content: "question", thinking: "", media: [], tools: [],
      },
    });
    state = reduceChatFrame(state, {
      type: "turn.started",
      request_id: "r1",
      session_id: "web:one",
      turn_id: "turn-1",
    });

    expect(state.messages.find((message) => message.role === "user")).toMatchObject({ turnId: "turn-1" });
  });

  it("does not inject a distant running draft into an explicitly replaced window", () => {
    const current = {
      ...initialChatState,
      sessionId: "web:one",
      messages: [{
        id: "latest", role: "user" as const, content: "latest", thinking: "", media: [], tools: [],
      }],
      sessionMessages: {
        "web:one": [{
          id: "latest", role: "user" as const, content: "latest", thinking: "", media: [], tools: [],
        }],
      },
      turnStates: {
        "web:one": { status: "running" as const, queuePosition: null, turnId: "turn-latest", requestId: "r1" },
      },
    };

    const next = reduceChatFrame(current, {
      type: "ui.session.select",
      sessionId: "web:one",
      replace: true,
      messages: [{
        id: "old", role: "user", content: "old", thinking: "", media: [], tools: [], turnId: "turn-old",
      }],
    });

    expect(next.messages.some((message) => message.id === "old")).toBe(true);
    expect(next.messages.some((message) => message.turnId === "turn-latest")).toBe(false);
  });

  it("无时间的流式草稿不会阻止提醒回到正确时间位置", () => {
    const timeline = mergeTimeline(
      [{
        id: "user-latest", role: "user", content: "晚上提问", thinking: "", media: [], tools: [],
        timestamp: "2026-07-20T20:00:00+08:00",
      }, {
        id: "draft", role: "assistant", content: "", thinking: "", media: [], tools: [],
        streaming: true,
      }],
      [{
        id: "notice-morning", role: "assistant", content: "早上提醒", thinking: "", media: [], tools: [],
        source: "scheduled_reminder", timestamp: "2026-07-20T10:00:00+08:00",
      }],
    );

    expect(timeline.map((message) => message.id)).toEqual([
      "notice-morning", "user-latest", "draft",
    ]);
  });

  it("运行中的用户消息和助手草稿始终带有时间", () => {
    let state = reduceChatFrame({ ...initialChatState, sessionId: "web:one" }, {
      type: "turn.snapshot",
      session_id: "web:one",
      turn_id: "turn-1",
      request_id: "r1",
      user_message: "继续测试",
      user_media: [],
      content: "处理中",
      thinking: "",
      tools: [],
      status: "running",
    });

    expect(state.messages).toHaveLength(2);
    expect(state.messages.every((message) => !Number.isNaN(Date.parse(message.timestamp || "")))).toBe(true);
  });

  it("主动历史消息只展示最终正文，不暴露持久化工具链", () => {
    const [message] = rowsToMessages([{
      id: "proactive-1",
      role: "assistant",
      content: "给你一道关于记忆去重的面试题。",
      proactive: true,
      reasoning_content: "不应展示",
      tool_chain: [{ calls: [{ call_id: "call-1", name: "recall_memory", arguments: {}, result: "偏好", status: "ok" }] }],
    }]);

    expect(message.thinking).toBe("");
    expect(message.tools).toEqual([]);
  });

  it("错误历史消息保留实际连接与模型路由", () => {
    const [message] = rowsToMessages([{
      id: "error-1",
      role: "assistant",
      content: "出错：模型调用失败",
      status: "error",
      metadata: { model_route: {
        connection_id: "company",
        connection_name: "公司 API",
        model_id: "model-a",
        model_display_name: "Model A",
        adapter: "generic_openai",
      } },
    }]);

    expect(message.modelRoute).toEqual({
      connection_id: "company",
      connection_name: "公司 API",
      model_id: "model-a",
      model_display_name: "Model A",
      adapter: "generic_openai",
    });
  });

  it("merges a fresh running snapshot when returning to an active session", () => {
    let state = reduceChatFrame({ ...initialChatState, sessionId: "web:one" }, {
      type: "ui.user.append",
      message: {
        id: "user-r1", role: "user", content: "inspect project", thinking: "", media: [], tools: [],
      },
    });
    state = reduceChatFrame(state, {
      type: "turn.started", request_id: "r1", session_id: "web:one", turn_id: "turn-1",
    });
    state = reduceChatFrame(state, {
      type: "answer.delta", session_id: "web:one", turn_id: "turn-1", delta: "stale partial",
    });
    state = reduceChatFrame(state, {
      type: "ui.session.select", sessionId: "web:two", messages: [],
    });

    state = reduceChatFrame(state, {
      type: "ui.session.select",
      sessionId: "web:one",
      messages: [{
        id: "running:user:turn-1",
        seq: -2,
        role: "user",
        content: "inspect project",
        thinking: "",
        media: [],
        tools: [],
        turnId: "turn-1",
        streaming: false,
        status: "running",
      }, {
        id: "running:assistant:turn-1",
        seq: -1,
        role: "assistant",
        content: "fresh snapshot with more output",
        thinking: "fresh thinking",
        media: [],
        tools: [],
        turnId: "turn-1",
        streaming: true,
        status: "running",
      }],
    });

    expect(state.messages.find((message) => message.role === "assistant")).toMatchObject({
      content: "fresh snapshot with more output",
      thinking: "fresh thinking",
      streaming: true,
    });
  });
});
