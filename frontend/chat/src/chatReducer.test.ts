import { describe, expect, it } from "vitest";

import { initialChatState, mergeTimeline, notificationRowsToMessages, reduceChatFrame } from "./chatReducer";

describe("reduceChatFrame", () => {
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
