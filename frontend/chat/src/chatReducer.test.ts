import { describe, expect, it } from "vitest";

import { initialChatState, reduceChatFrame } from "./chatReducer";

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
});
