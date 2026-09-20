import { describe, expect, it } from "vitest";

import { initialChatState, mergeTimeline, normalizeToolStatus, notificationRowsToMessages, reduceChatFrame, rowsToMessages } from "./chatReducer";

describe("reduceChatFrame", () => {
  it("缺少 turn_id 的旧 final 帧不会删除当前 Turn 以外的审批", () => {
    let state = reduceChatFrame({ ...initialChatState, sessionId: "web:legacy-final-race" }, {
      type: "turn.started", request_id: "new-request", session_id: "web:legacy-final-race", turn_id: "turn-new",
    });
    const approval = {
      id: "approval-new-final", session_id: "web:legacy-final-race", turn_id: "turn-new", call_id: "call-new-final",
      tool_name: "shell", operation: "执行命令", arguments: { command: "echo hi" }, reason: "需要确认",
      requested_mode: "read-only" as const, state: "pending" as const, created_at: "2026-09-20T08:00:00.100Z",
    };
    state = reduceChatFrame(state, { type: "approval.requested", session_id: approval.session_id, approval });

    const unchanged = reduceChatFrame(state, {
      type: "message.final", request_id: "old-request", session_id: "web:legacy-final-race", turn_id: "",
      content: "旧结果",
    });

    expect(unchanged.turnStates["web:legacy-final-race"]).toMatchObject({ status: "running", turnId: "turn-new" });
    expect(unchanged.approvalRequests?.["web:legacy-final-race"]?.["approval-new-final"]).toBeTruthy();
  });

  it("保留工具生命周期时间并在未知 wire 状态下 fail-safe", () => {
    expect(normalizeToolStatus("ok")).toBe("completed");
    expect(normalizeToolStatus("未来的新状态")).toBe("unknown");
    const [history] = rowsToMessages([{
      id: "timed", role: "assistant", content: "完成", turn_id: "turn-timed",
      tool_chain: [{ calls: [{
        call_id: "call-timed", name: "read_file", status: "ok", arguments: { path: "README.md" }, result: "done",
        started_at: "2026-09-20T08:00:00.000Z", ended_at: "2026-09-20T08:00:01.250Z", duration_ms: 1250,
      }, { call_id: "call-unknown", name: "future_tool", status: "future_state", result: "" }] }],
    }]);
    expect(history.tools).toMatchObject([
      { callId: "call-timed", status: "completed", startedAt: "2026-09-20T08:00:00.000Z", endedAt: "2026-09-20T08:00:01.250Z", durationMs: 1250 },
      { callId: "call-unknown", status: "unknown" },
    ]);
  });

  it("运行工具收到终态后不会被迟到 started 事件降级", () => {
    let state = reduceChatFrame({ ...initialChatState, sessionId: "web:timing" }, {
      type: "turn.started", request_id: "r-timing", session_id: "web:timing", turn_id: "turn-timing",
    });
    state = reduceChatFrame(state, {
      type: "react.tool.started", session_id: "web:timing", turn_id: "turn-timing", call_id: "call-1", tool_name: "search", arguments: {},
      started_at: "2026-09-20T08:00:00.000Z",
    });
    state = reduceChatFrame(state, {
      type: "react.tool.completed", session_id: "web:timing", turn_id: "turn-timing", call_id: "call-1", tool_name: "search", status: "ok", result_preview: "done",
      ended_at: "2026-09-20T08:00:00.600Z", duration_ms: 600, result_kind: "text", is_truncated: true, exit_code: 7,
      error_code: "partial_output",
    });
    state = reduceChatFrame(state, {
      type: "react.tool.started", session_id: "web:timing", turn_id: "turn-timing", call_id: "call-1", tool_name: "search", arguments: {},
    });
    expect(state.messages[0].tools[0]).toMatchObject({ status: "completed", durationMs: 600, startedAt: "2026-09-20T08:00:00.000Z", resultKind: "text", isTruncated: true, exitCode: 7, errorCode: "partial_output" });
  });

  it("把审批 requested/resolved 绑定到对应工具并阻止拒绝后的迟到完成帧", () => {
    let state = reduceChatFrame({ ...initialChatState, sessionId: "web:approval" }, {
      type: "turn.started", request_id: "r-approval", session_id: "web:approval", turn_id: "turn-approval",
    });
    state = reduceChatFrame(state, {
      type: "react.tool.started", session_id: "web:approval", turn_id: "turn-approval", call_id: "call-approval", tool_name: "write_file", arguments: {},
      started_at: "2026-09-20T08:00:00.000Z",
    });
    const approval = {
      id: "approval-1", session_id: "web:approval", turn_id: "turn-approval", call_id: "call-approval",
      tool_name: "write_file", operation: "写入文件", arguments: { path: "outside.txt" }, reason: "超出范围",
      requested_mode: "read-only" as const, fingerprint: "fp", state: "pending" as const,
      created_at: "2026-09-20T08:00:00.100Z",
    };
    state = reduceChatFrame(state, { type: "approval.requested", session_id: "web:approval", approval });
    expect(state.messages[0].tools[0]).toMatchObject({ approvalId: "approval-1", approvalState: "pending", approvalRequestedAt: approval.created_at });
    state = reduceChatFrame(state, {
      type: "approval.resolved", request_id: "r-resolve", session_id: "web:approval", approval_id: "approval-1",
      decision: "rejected", decided_at: "2026-09-20T08:00:00.400Z",
    });
    expect(state.messages[0].tools[0]).toMatchObject({ status: "rejected", approvalState: "rejected", approvalWaitMs: 300 });
    state = reduceChatFrame(state, {
      type: "react.tool.completed", session_id: "web:approval", turn_id: "turn-approval", call_id: "call-approval", tool_name: "write_file", status: "ok", result_preview: "should not win",
      ended_at: "2026-09-20T08:00:01.000Z", duration_ms: 1000,
    });
    expect(state.messages[0].tools[0]).toMatchObject({ status: "rejected", resultPreview: "" });
  });

  it("未知工具状态收到审批请求时仍保留可操作的审批关联", () => {
    let state = reduceChatFrame({ ...initialChatState, sessionId: "web:unknown-tool-approval" }, {
      type: "turn.started", request_id: "r-unknown-tool", session_id: "web:unknown-tool-approval", turn_id: "turn-unknown-tool",
    });
    state = reduceChatFrame(state, {
      type: "react.tool.completed", session_id: "web:unknown-tool-approval", turn_id: "turn-unknown-tool",
      call_id: "call-unknown-tool", tool_name: "write_file", status: "future_state", result_preview: "",
    });
    const approval = {
      id: "approval-unknown-tool", session_id: "web:unknown-tool-approval", turn_id: "turn-unknown-tool", call_id: "call-unknown-tool",
      tool_name: "write_file", operation: "写入文件", arguments: { path: "outside.txt" }, reason: "超出范围",
      requested_mode: "read-only" as const, state: "pending" as const, created_at: "2026-09-20T08:00:00.100Z",
    };
    state = reduceChatFrame(state, { type: "approval.requested", session_id: approval.session_id, approval });

    expect(state.messages[0].tools[0]).toMatchObject({ status: "unknown", approvalState: "pending", approvalId: approval.id });
    expect(state.approvalRequests?.[approval.session_id]?.[approval.id]).toEqual(approval);
  });

  it("resolved 先到时不会在 requested replay 后重新显示等待，并能关联迟到 started", () => {
    let state = { ...initialChatState, sessionId: "web:late-approval" };
    state = reduceChatFrame(state, {
      type: "approval.resolved", request_id: "resolve-1", session_id: "web:late-approval",
      approval_id: "approval-late", decision: "rejected", turn_id: "turn-late", call_id: "call-late",
      decided_at: "2026-09-20T08:00:01.000Z", error_code: "user_rejected",
    });
    state = reduceChatFrame(state, {
      type: "approval.requested", session_id: "web:late-approval", approval: {
        id: "approval-late", session_id: "web:late-approval", turn_id: "turn-late", call_id: "call-late",
        tool_name: "write_file", operation: "写入文件", arguments: { path: "outside.txt" }, reason: "超出范围",
        requested_mode: "read-only", fingerprint: "fp", state: "pending", created_at: "2026-09-20T08:00:00.500Z",
      },
    });
    state = reduceChatFrame(state, {
      type: "react.tool.started", session_id: "web:late-approval", turn_id: "turn-late", call_id: "call-late",
      tool_name: "write_file", arguments: { path: "outside.txt" }, started_at: "2026-09-20T08:00:00.600Z",
    });

    expect(state.approvalRequests).toEqual({});
    expect(state.messages[0].tools[0]).toMatchObject({
      callId: "call-late", status: "rejected", approvalState: "rejected",
      approvalId: "approval-late", approvalResolvedAt: "2026-09-20T08:00:01.000Z",
      errorCode: "user_rejected",
    });
  });

  it("旧回执缺少 turn/call 时由 requested replay 补齐关联", () => {
    let state = { ...initialChatState, sessionId: "web:legacy-approval" };
    state = reduceChatFrame(state, {
      type: "approval.resolved", request_id: "resolve-legacy", session_id: "web:legacy-approval",
      approval_id: "approval-legacy", decision: "rejected", decided_at: "2026-09-20T08:00:01.000Z",
    });
    state = reduceChatFrame(state, {
      type: "approval.requested", session_id: "web:legacy-approval", approval: {
        id: "approval-legacy", session_id: "web:legacy-approval", turn_id: "turn-legacy", call_id: "call-legacy",
        tool_name: "write_file", operation: "写入文件", arguments: { path: "outside.txt" }, reason: "超出范围",
        requested_mode: "read-only", fingerprint: "fp", state: "pending", created_at: "2026-09-20T08:00:00.500Z",
      },
    });
    state = reduceChatFrame(state, {
      type: "react.tool.started", session_id: "web:legacy-approval", turn_id: "turn-legacy", call_id: "call-legacy",
      tool_name: "write_file", arguments: { path: "outside.txt" },
    });

    expect(state.resolvedApprovals?.["web:legacy-approval"]?.["approval-legacy"]).toMatchObject({
      turn_id: "turn-legacy", call_id: "call-legacy", requested_at: "2026-09-20T08:00:00.500Z",
    });
    expect(state.messages[0].tools[0]).toMatchObject({ status: "rejected", approvalState: "rejected" });
  });

  it("未知审批终态按不可用收敛，不会被误判为已允许本次", () => {
    let state = { ...initialChatState, sessionId: "web:unknown-approval" };
    state = reduceChatFrame(state, {
      type: "approval.resolved", request_id: "resolve-unknown", session_id: "web:unknown-approval",
      approval_id: "approval-unknown", decision: "future-decision" as never,
      state: "future-state" as never, turn_id: "turn-unknown-approval", call_id: "call-unknown-approval",
      decided_at: "2026-09-20T08:00:01.000Z",
    });
    state = reduceChatFrame(state, {
      type: "react.tool.started", session_id: "web:unknown-approval", turn_id: "turn-unknown-approval",
      call_id: "call-unknown-approval", tool_name: "write_file", arguments: {},
    });

    expect(state.messages[0].tools[0]).toMatchObject({
      status: "unavailable",
      approvalState: "unavailable",
      approvalId: "approval-unknown",
    });
    expect(state.resolvedApprovals?.["web:unknown-approval"]?.["approval-unknown"]?.decision).toBeNull();
  });

  it("缺少审批 state 和 decision 时按不可用收敛", () => {
    let state = { ...initialChatState, sessionId: "web:missing-approval" };
    state = reduceChatFrame(state, {
      type: "approval.resolved", request_id: "resolve-missing", session_id: "web:missing-approval",
      approval_id: "approval-missing", turn_id: "turn-missing-approval", call_id: "call-missing-approval",
      decided_at: "2026-09-20T08:00:01.000Z",
    });
    state = reduceChatFrame(state, {
      type: "react.tool.started", session_id: "web:missing-approval", turn_id: "turn-missing-approval",
      call_id: "call-missing-approval", tool_name: "write_file", arguments: {},
    });

    expect(state.messages[0].tools[0]).toMatchObject({ status: "unavailable", approvalState: "unavailable" });
    expect(state.resolvedApprovals?.["web:missing-approval"]?.["approval-missing"]?.decision).toBeNull();
  });

  it("非终态审批回执不会留下没有操作入口的等待状态", () => {
    let state = { ...initialChatState, sessionId: "web:pending-resolution" };
    state = reduceChatFrame(state, {
      type: "approval.resolved", request_id: "resolve-pending", session_id: "web:pending-resolution",
      approval_id: "approval-pending-resolution", state: "pending", decision: undefined,
      turn_id: "turn-pending-resolution", call_id: "call-pending-resolution",
      decided_at: "2026-09-20T08:00:01.000Z",
    });
    state = reduceChatFrame(state, {
      type: "react.tool.started", session_id: "web:pending-resolution", turn_id: "turn-pending-resolution",
      call_id: "call-pending-resolution", tool_name: "write_file", arguments: {},
    });

    expect(state.messages[0].tools[0]).toMatchObject({
      status: "unavailable",
      approvalState: "unavailable",
    });
  });

  it("相同 approval id 在不同 session 中不会串联", () => {
    let state = { ...initialChatState, sessionId: "web:session-a" };
    state = reduceChatFrame(state, {
      type: "approval.resolved", request_id: "resolve-a", session_id: "web:session-a",
      approval_id: "approval-shared", decision: "rejected", turn_id: "turn-a", call_id: "call-a",
    });
    state = reduceChatFrame(state, {
      type: "react.tool.started", session_id: "web:session-b", turn_id: "turn-b", call_id: "call-b",
      tool_name: "write_file", arguments: { path: "b.txt" },
    });
    expect(state.sessionMessages["web:session-b"]?.[0].tools[0]).toMatchObject({
      status: "running",
    });
  });

  it("相同 approval id 的 pending 请求按 session 隔离保存", () => {
    let state = { ...initialChatState, sessionId: "web:approval-a" };
    const approval = (sessionId: string, turnId: string, callId: string) => ({
      id: "approval-shared-pending", session_id: sessionId, turn_id: turnId, call_id: callId,
      tool_name: "write_file", operation: "写入文件", arguments: { path: `${sessionId}.txt` }, reason: "超出范围",
      requested_mode: "read-only" as const, fingerprint: `fp-${sessionId}`, state: "pending" as const,
      created_at: "2026-09-20T08:00:00.000Z",
    });
    state = reduceChatFrame(state, {
      type: "approval.requested", session_id: "web:approval-a",
      approval: approval("web:approval-a", "turn-a", "call-a"),
    });
    state = reduceChatFrame(state, {
      type: "approval.requested", session_id: "web:approval-b",
      approval: approval("web:approval-b", "turn-b", "call-b"),
    });
    state = reduceChatFrame(state, {
      type: "react.tool.started", session_id: "web:approval-a", turn_id: "turn-a", call_id: "call-a",
      tool_name: "write_file", arguments: { path: "a.txt" },
    });
    state = reduceChatFrame(state, {
      type: "react.tool.started", session_id: "web:approval-b", turn_id: "turn-b", call_id: "call-b",
      tool_name: "write_file", arguments: { path: "b.txt" },
    });

    expect(state.approvalRequests?.["web:approval-a"]?.["approval-shared-pending"]).toBeTruthy();
    expect(state.approvalRequests?.["web:approval-b"]?.["approval-shared-pending"]).toBeTruthy();
    expect(state.sessionMessages["web:approval-a"]?.[0].tools[0]).toMatchObject({ approvalState: "pending" });
    expect(state.sessionMessages["web:approval-b"]?.[0].tools[0]).toMatchObject({ approvalState: "pending" });

    state = reduceChatFrame(state, {
      type: "approval.resolved", request_id: "resolve-a", session_id: "web:approval-a",
      approval_id: "approval-shared-pending", decision: "rejected", turn_id: "turn-a", call_id: "call-a",
    });
    expect(state.approvalRequests?.["web:approval-a"]).toBeUndefined();
    expect(state.approvalRequests?.["web:approval-b"]?.["approval-shared-pending"]).toBeTruthy();
  });

  it("拒绝回执先到时迟到 completed 的结果不会被展示", () => {
    let state = { ...initialChatState, sessionId: "web:reject-before-complete" };
    state = reduceChatFrame(state, {
      type: "approval.resolved", request_id: "resolve-before-complete", session_id: "web:reject-before-complete",
      approval_id: "approval-before-complete", decision: "rejected", turn_id: "turn-before-complete", call_id: "call-before-complete",
    });
    state = reduceChatFrame(state, {
      type: "react.tool.completed", session_id: "web:reject-before-complete", turn_id: "turn-before-complete",
      call_id: "call-before-complete", tool_name: "write_file", status: "ok", result_preview: "should be hidden",
    });

    expect(state.messages[0].tools[0]).toMatchObject({ status: "rejected", resultPreview: "" });
  });

  it("工具已在拒绝回执前完成时不因迟到回执回退其终态", () => {
    let state = reduceChatFrame({ ...initialChatState, sessionId: "web:completed-first" }, {
      type: "turn.started", request_id: "r-completed-first", session_id: "web:completed-first", turn_id: "turn-completed-first",
    });
    state = reduceChatFrame(state, {
      type: "react.tool.completed", session_id: "web:completed-first", turn_id: "turn-completed-first",
      call_id: "call-completed-first", tool_name: "write_file", status: "ok", result_preview: "done",
      ended_at: "2026-09-20T08:00:01.000Z",
    });
    state = reduceChatFrame(state, {
      type: "approval.resolved", request_id: "resolve-late", session_id: "web:completed-first",
      approval_id: "approval-late-completed", decision: "rejected", turn_id: "turn-completed-first",
      call_id: "call-completed-first", decided_at: "2026-09-20T08:00:02.000Z",
    });

    expect(state.messages[0].tools[0]).toMatchObject({ status: "completed", resultPreview: "done" });
  });

  it("旧 completed 帧缺少结束时间时不因迟到拒绝回执回退", () => {
    let state = reduceChatFrame({ ...initialChatState, sessionId: "web:completed-without-time" }, {
      type: "turn.started", request_id: "r-completed-without-time", session_id: "web:completed-without-time", turn_id: "turn-completed-without-time",
    });
    state = reduceChatFrame(state, {
      type: "react.tool.completed", session_id: "web:completed-without-time", turn_id: "turn-completed-without-time",
      call_id: "call-completed-without-time", tool_name: "write_file", status: "ok", result_preview: "done",
    });
    state = reduceChatFrame(state, {
      type: "approval.resolved", request_id: "resolve-late", session_id: "web:completed-without-time",
      approval_id: "approval-late", decision: "rejected", turn_id: "turn-completed-without-time",
      call_id: "call-completed-without-time", decided_at: "2026-09-20T08:00:02.000Z",
    });

    expect(state.messages[0].tools[0]).toMatchObject({ status: "completed", resultPreview: "done" });
  });

  it("没有 status 的已结束 final 不会被迟到 started 或 snapshot 重新激活", () => {
    let state = reduceChatFrame({ ...initialChatState, sessionId: "web:closed-without-status" }, {
      type: "turn.started", request_id: "r-closed-without-status", session_id: "web:closed-without-status", turn_id: "turn-closed-without-status",
    });
    state = reduceChatFrame(state, {
      type: "message.final", session_id: "web:closed-without-status", turn_id: "turn-closed-without-status", content: "完成",
    });
    state = reduceChatFrame(state, {
      type: "turn.started", request_id: "r-late", session_id: "web:closed-without-status", turn_id: "turn-closed-without-status",
    });
    state = reduceChatFrame(state, {
      type: "turn.snapshot", request_id: "r-late", session_id: "web:closed-without-status", turn_id: "turn-closed-without-status", status: "running",
    });

    expect(state.messages[0]).toMatchObject({ content: "完成", streaming: false });
    expect(state.turnStates["web:closed-without-status"]?.status).toBe("idle");
  });

  it("迟到 approval.requested 不能把已完成工具重新打开为 pending", () => {
    let state = reduceChatFrame({ ...initialChatState, sessionId: "web:requested-late" }, {
      type: "turn.started", request_id: "r-requested-late", session_id: "web:requested-late", turn_id: "turn-requested-late",
    });
    state = reduceChatFrame(state, {
      type: "react.tool.completed", session_id: "web:requested-late", turn_id: "turn-requested-late",
      call_id: "call-requested-late", tool_name: "write_file", status: "ok", result_preview: "done",
      ended_at: "2026-09-20T08:00:01.000Z",
    });
    state = reduceChatFrame(state, {
      type: "approval.requested", session_id: "web:requested-late", approval: {
        id: "approval-requested-late", session_id: "web:requested-late", turn_id: "turn-requested-late",
        call_id: "call-requested-late", tool_name: "write_file", operation: "写入文件", arguments: { path: "outside.txt" },
        reason: "超出范围", requested_mode: "read-only", fingerprint: "fp", state: "pending",
        created_at: "2026-09-20T08:00:00.100Z",
      },
    });

    expect(state.messages[0].tools[0]).toMatchObject({ status: "completed", approvalId: "approval-requested-late" });
    expect(state.messages[0].tools[0].approvalState).not.toBe("pending");
    expect(state.approvalRequests).toEqual({});
  });

  it("旧 Turn 的 final 不会清空新 Turn 的运行态", () => {
    let state = { ...initialChatState, sessionId: "web:turn-epoch" };
    state = reduceChatFrame(state, {
      type: "turn.started", request_id: "r-old", session_id: "web:turn-epoch", turn_id: "turn-old",
    });
    state = reduceChatFrame(state, {
      type: "turn.started", request_id: "r-new", session_id: "web:turn-epoch", turn_id: "turn-new",
    });
    state = reduceChatFrame(state, {
      type: "message.final", session_id: "web:turn-epoch", turn_id: "turn-old", content: "旧回复",
    });

    expect(state.turnStates["web:turn-epoch"]).toMatchObject({ status: "running", turnId: "turn-new" });
    expect(state.activeTurnId).toBe("turn-new");
  });

  it("旧 Turn 的 interrupted 不会清空新 Turn 的运行态", () => {
    let state = { ...initialChatState, sessionId: "web:interrupt-epoch" };
    state = reduceChatFrame(state, {
      type: "turn.started", request_id: "r-old", session_id: "web:interrupt-epoch", turn_id: "turn-old",
    });
    state = reduceChatFrame(state, {
      type: "turn.started", request_id: "r-new", session_id: "web:interrupt-epoch", turn_id: "turn-new",
    });
    state = reduceChatFrame(state, {
      type: "turn.interrupted", request_id: "stop-old", session_id: "web:interrupt-epoch",
      turn_id: "turn-old", status: "interrupted", ended_at: "2026-09-20T08:00:01.000Z",
    });

    expect(state.turnStates["web:interrupt-epoch"]).toMatchObject({ status: "running", turnId: "turn-new" });
    expect(state.activeTurnId).toBe("turn-new");
  });

  it("final 之后的迟到正文和思考增量不会重新打开消息", () => {
    let state = reduceChatFrame({ ...initialChatState, sessionId: "web:late-delta" }, {
      type: "turn.started", request_id: "r-late-delta", session_id: "web:late-delta", turn_id: "turn-late-delta",
    });
    state = reduceChatFrame(state, {
      type: "message.final", session_id: "web:late-delta", turn_id: "turn-late-delta", content: "最终内容", thinking: "已完成",
    });
    const afterFinal = state;
    state = reduceChatFrame(state, {
      type: "answer.delta", session_id: "web:late-delta", turn_id: "turn-late-delta", delta: "迟到正文",
    });
    state = reduceChatFrame(state, {
      type: "react.thinking.delta", session_id: "web:late-delta", turn_id: "turn-late-delta", delta: "迟到思考",
    });

    expect(state).toBe(afterFinal);
  });

  it("工具 completed 先于 started 时仍保留终态和结果", () => {
    let state = reduceChatFrame({ ...initialChatState, sessionId: "web:out-of-order" }, {
      type: "turn.started", request_id: "r-order", session_id: "web:out-of-order", turn_id: "turn-order",
    });
    state = reduceChatFrame(state, {
      type: "react.tool.completed", session_id: "web:out-of-order", turn_id: "turn-order", call_id: "call-order",
      tool_name: "search", status: "ok", result_preview: "done", started_at: "2026-09-20T08:00:00.000Z",
      ended_at: "2026-09-20T08:00:00.200Z", duration_ms: 200,
    });
    state = reduceChatFrame(state, {
      type: "react.tool.started", session_id: "web:out-of-order", turn_id: "turn-order", call_id: "call-order",
      tool_name: "search", arguments: { query: "bean" }, started_at: "2026-09-20T08:00:00.000Z",
    });

    expect(state.messages[0].tools[0]).toMatchObject({
      callId: "call-order", status: "completed", resultPreview: "done", durationMs: 200,
      arguments: { query: "bean" },
    });
  });

  it("不同终态乱序时按结束时间保留较新的事件", () => {
    let state = reduceChatFrame({ ...initialChatState, sessionId: "web:terminal-order" }, {
      type: "turn.started", request_id: "r-terminal-order", session_id: "web:terminal-order", turn_id: "turn-terminal-order",
    });
    state = reduceChatFrame(state, {
      type: "react.tool.completed", session_id: "web:terminal-order", turn_id: "turn-terminal-order",
      call_id: "call-terminal-order", tool_name: "shell", status: "ok", result_preview: "done",
      ended_at: "2026-09-20T08:00:02.000Z",
    });
    state = reduceChatFrame(state, {
      type: "react.tool.completed", session_id: "web:terminal-order", turn_id: "turn-terminal-order",
      call_id: "call-terminal-order", tool_name: "shell", status: "error", result_preview: "late old error",
      ended_at: "2026-09-20T08:00:01.000Z",
    });
    expect(state.messages[0].tools[0]).toMatchObject({ status: "completed", resultPreview: "done" });
  });

  it("后台会话的工具帧即使缺少 turn.started 也会保留，而普通陌生增量仍忽略", () => {
    let state = { ...initialChatState, sessionId: "web:current" };
    state = reduceChatFrame(state, {
      type: "react.tool.completed", session_id: "web:background", turn_id: "turn-background",
      call_id: "call-background", tool_name: "search", status: "ok", result_preview: "done",
    });
    expect(state.sessionMessages["web:background"]?.[0].tools[0]).toMatchObject({ status: "completed" });
    const unchanged = reduceChatFrame(state, {
      type: "answer.delta", session_id: "web:unknown", turn_id: "turn-unknown", delta: "不应出现",
    });
    expect(unchanged).toBe(state);
  });

  it("中断会收敛运行工具并计算工具自身耗时", () => {
    let state = reduceChatFrame({ ...initialChatState, sessionId: "web:interrupt-tool" }, {
      type: "turn.started", request_id: "r-interrupt", session_id: "web:interrupt-tool", turn_id: "turn-interrupt",
    });
    state = reduceChatFrame(state, {
      type: "react.tool.started", session_id: "web:interrupt-tool", turn_id: "turn-interrupt", call_id: "call-interrupt",
      tool_name: "shell", arguments: { command: "sleep 10" }, started_at: "2026-09-20T08:00:00.000Z",
    });
    state = reduceChatFrame(state, {
      type: "turn.interrupted", request_id: "stop-interrupt", session_id: "web:interrupt-tool",
      turn_id: "turn-interrupt", status: "interrupted", ended_at: "2026-09-20T08:00:02.250Z",
    });

    expect(state.messages[0].tools[0]).toMatchObject({
      status: "interrupted", endedAt: "2026-09-20T08:00:02.250Z", durationMs: 2250,
      errorCode: "turn_interrupted",
    });
  });

  it("取消 Turn 时工具显示 cancelled 而不是继续等待", () => {
    let state = reduceChatFrame({ ...initialChatState, sessionId: "web:cancel-tool" }, {
      type: "turn.started", request_id: "r-cancel", session_id: "web:cancel-tool", turn_id: "turn-cancel",
    });
    state = reduceChatFrame(state, {
      type: "react.tool.started", session_id: "web:cancel-tool", turn_id: "turn-cancel", call_id: "call-cancel",
      tool_name: "shell", arguments: { command: "sleep 10" }, started_at: "2026-09-20T08:00:00.000Z",
    });
    state = reduceChatFrame(state, {
      type: "turn.interrupted", request_id: "stop-cancel", session_id: "web:cancel-tool",
      turn_id: "turn-cancel", status: "cancelled", ended_at: "2026-09-20T08:00:00.100Z",
    });

    expect(state.messages[0].tools[0].status).toBe("cancelled");
  });

  it("Turn 中断会收敛未知工具状态", () => {
    let state = reduceChatFrame({ ...initialChatState, sessionId: "web:interrupt-unknown" }, {
      type: "turn.started", request_id: "r-unknown", session_id: "web:interrupt-unknown", turn_id: "turn-unknown",
    });
    state = reduceChatFrame(state, {
      type: "react.tool.completed", session_id: "web:interrupt-unknown", turn_id: "turn-unknown",
      call_id: "call-unknown", tool_name: "future_tool", status: "future_status", result_preview: "",
      started_at: "2026-09-20T08:00:00.000Z",
    });
    state = reduceChatFrame(state, {
      type: "turn.interrupted", request_id: "stop-unknown", session_id: "web:interrupt-unknown",
      turn_id: "turn-unknown", status: "interrupted", ended_at: "2026-09-20T08:00:00.500Z",
    });

    expect(state.messages[0].tools[0]).toMatchObject({ status: "interrupted", endedAt: "2026-09-20T08:00:00.500Z", errorCode: "turn_interrupted" });
  });
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
      tools: [{ callId: "call-1", status: "interrupted", errorCode: "turn_interrupted" }],
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

  it("不完整的 turn snapshot 缺少工具列表时仍可恢复", () => {
    const state = reduceChatFrame({ ...initialChatState, sessionId: "web:partial-snapshot" }, {
      type: "turn.snapshot",
      session_id: "web:partial-snapshot",
      turn_id: "turn-partial-snapshot",
      request_id: "r-partial-snapshot",
      user_message: "恢复中的请求",
      content: "部分结果",
      thinking: "",
      status: "running",
    });

    expect(state.messages).toHaveLength(2);
    expect(state.messages[1]).toMatchObject({ turnId: "turn-partial-snapshot", tools: [], streaming: true });
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
