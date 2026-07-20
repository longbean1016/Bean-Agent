import { describe, expect, it } from "vitest";

import { initialChatState, mergeTimeline, notificationRowsToMessages, reduceChatFrame } from "./chatReducer";

describe("reduceChatFrame", () => {
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
});
