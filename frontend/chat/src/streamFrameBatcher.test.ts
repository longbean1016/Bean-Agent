import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { StreamFrameBatcher } from "./streamFrameBatcher";
import { initialChatState, reduceChatFrame } from "./chatReducer";
import type { ChatFrame, ChatMessage } from "./types";

beforeEach(() => vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "requestAnimationFrame", "cancelAnimationFrame"] }));
afterEach(() => vi.useRealTimers());
const delta = (text: string, session = "web:test", turn = "turn"): ChatFrame => ({ type: "answer.delta", session_id: session, turn_id: turn, delta: text });

it("按绘制帧合并中文和跨分片 Markdown，不添加逐字延迟", () => {
  const emit = vi.fn();
  const batcher = new StreamFrameBatcher(emit);
  const parts = ["你好", "\n\n```", "ts\nconst x = 1;", "\n```\n", "| a | b |\n|---|---|\n", "|1|2|", "😀"];
  parts.forEach((text) => batcher.push(delta(text)));
  expect(emit).not.toHaveBeenCalled();
  vi.advanceTimersToNextFrame();
  expect(emit).toHaveBeenCalledExactlyOnceWith(delta(parts.join("")));
  vi.advanceTimersByTime(100);
  expect(emit).toHaveBeenCalledTimes(1);
});

it.each(["message.final", "turn.interrupted", "approval.requested", "react.tool.started", "error", "turn.snapshot"])("%s 即时处理，先补齐文本且旧批次不能覆盖终态", (type) => {
  const emit = vi.fn();
  const batcher = new StreamFrameBatcher(emit);
  batcher.push(delta("尾字"));
  // 此测试只检查调度顺序，具体协议字段由 reducer 和集成测试覆盖。
  const control = { type } as ChatFrame;
  batcher.push(control);
  expect(emit.mock.calls.map(([frame]) => frame)).toEqual([delta("尾字"), control]);
  vi.advanceTimersByTime(100);
  expect(emit).toHaveBeenCalledTimes(2);
});

it("不同会话、Turn、思考与正文不能拼接或改变顺序", () => {
  const emit = vi.fn();
  const batcher = new StreamFrameBatcher(emit);
  const frames: ChatFrame[] = [delta("A"), delta("B", "web:other"), delta("C", "web:other", "next"),
    { type: "react.thinking.delta", session_id: "web:other", turn_id: "next", delta: "思考" }, delta("D")];
  frames.forEach((frame) => batcher.push(frame));
  batcher.flush();
  expect(emit.mock.calls.map(([frame]) => frame)).toEqual(frames);
});

it("绘制暂停时有定时回退，取消后旧帧不会再更新页面", () => {
  const raf = vi.spyOn(window, "requestAnimationFrame").mockReturnValue(9);
  const emit = vi.fn();
  const batcher = new StreamFrameBatcher(emit);
  batcher.push(delta("后台"));
  vi.advanceTimersByTime(50);
  expect(emit).toHaveBeenCalledExactlyOnceWith(delta("后台"));
  batcher.push(delta("卸载"));
  batcher.cancel();
  vi.advanceTimersByTime(100);
  expect(emit).toHaveBeenCalledTimes(1);
  raf.mockRestore();
});

it("模拟长历史：320 个分片合并为不超过 22 次状态更新，结果与逐片处理一致", () => {
  const history: ChatMessage[] = Array.from({ length: 1000 }, (_, index) => ({
    id: `history-${index}`, role: index % 2 ? "assistant" : "user", turnId: `old-${Math.floor(index / 2)}`,
    content: "模拟历史段落", thinking: "", tools: [], media: [],
  }));
  const selected = reduceChatFrame(initialChatState, { type: "ui.session.select", sessionId: "web:test", messages: history });
  const initial = reduceChatFrame(selected, { type: "turn.started", session_id: "web:test", turn_id: "turn" });
  let direct = initial;
  let batched = initial;
  let batchCount = 0;
  let directMs = 0;
  let batchedMs = 0;
  const batcher = new StreamFrameBatcher((frame) => {
    const start = performance.now();
    batched = reduceChatFrame(batched, frame);
    batchedMs += performance.now() - start;
    batchCount += 1;
  });
  for (let index = 0; index < 320; index += 1) {
    const frame = delta(index % 8 ? "文字" : "\n\n");
    const start = performance.now();
    direct = reduceChatFrame(direct, frame);
    directMs += performance.now() - start;
    batcher.push(frame);
    vi.advanceTimersByTime(1);
  }
  batcher.flush();
  expect(batched.messages).toEqual(direct.messages);
  expect(batched.messages[0]).toBe(history[0]);
  expect(batchCount).toBeLessThanOrEqual(22);
  console.info(JSON.stringify({ fragments: 320, historyMessages: 1000, directUpdates: 320, batchedUpdates: batchCount,
    directReducerMs: +directMs.toFixed(2), batchedReducerMs: +batchedMs.toFixed(2) }));
});
