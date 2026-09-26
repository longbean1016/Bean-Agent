import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { TurnProcess } from "./TurnProcess";
import { messageProcess, readPresentation, type TurnPresentation } from "./turnPresentation";
import { initialChatState, reduceChatFrame, rowsToMessages } from "./chatReducer";
import type { ChatMessage, ChatState } from "./types";

afterEach(() => { cleanup(); vi.useRealTimers(); });
const presentation: TurnPresentation = { version: 1, started_at: "2026-09-25T10:00:00Z", parts: [
  { id: "m", kind: "memory", query: "夜跑", items: [{ id: "m1", summary: "喜欢晚上跑步" }], duration_ms: 200 },
  { id: "r", kind: "thinking", text: "先查看天气" },
  { id: "t", kind: "text", text: "先查实时信息。" },
  { id: "c", kind: "tool", call_id: "skill" },
] };
const message: ChatMessage = { id: "turn", turnId: "turn", role: "assistant", content: "", thinking: "先查看天气", media: [],
  streaming: true, presentation, tools: [{ callId: "skill", name: "load_skill", arguments: { name: "weather" }, status: "completed", resultPreview: "天气指引" }] };
const renderText = (text: string) => <p>{text}</p>;
const props = () => ({ message, parts: presentation.parts, disclosure: new Map<string, boolean>(), disclosureKey: "session:turn",
  approvals: [], approvalDecisionRequests: {}, renderText });

it("过程按顺序显示且思考/记忆/技能均默认收起，终答时统一折叠", () => {
  const initial = props();
  const { container, rerender } = render(<TurnProcess {...initial} />);
  expect(screen.getByRole("button", { name: /工作中/ })).toHaveAttribute("aria-expanded", "true");
  expect(screen.queryByText("先查看天气")).not.toBeInTheDocument();
  expect(screen.queryByText("喜欢晚上跑步")).not.toBeInTheDocument();
  expect(screen.queryByText("天气指引")).not.toBeInTheDocument();
  expect(container.textContent!.indexOf("已检索记忆")).toBeLessThan(container.textContent!.indexOf("思考"));
  expect(container.textContent!.indexOf("先查实时信息")).toBeLessThan(container.textContent!.indexOf("使用了技能"));
  fireEvent.click(screen.getByRole("button", { name: /已检索记忆/ }));
  expect(screen.getByText("喜欢晚上跑步")).toBeVisible();
  const final = { ...message, presentation: { ...presentation, final: true } };
  rerender(<TurnProcess {...initial} message={final} />);
  const trigger = screen.getByRole("button", { name: /工作中/ });
  expect(trigger).toHaveAttribute("aria-expanded", "false");
  fireEvent.click(trigger);
  rerender(<TurnProcess {...initial} message={{ ...final, content: "回答继续增长" }} />);
  expect(trigger).toHaveAttribute("aria-expanded", "true");
  rerender(<TurnProcess {...initial} message={{ ...final, streaming: false, durationMs: 4200 }} />);
  expect(screen.getByRole("button", { name: "已工作 4.2 秒" })).toHaveAttribute("aria-expanded", "true");
});

it("工作时钟包含正文生成时间，完成后冻结；历史缺时长不编造", () => {
  vi.useFakeTimers(); vi.setSystemTime(new Date("2026-09-25T10:00:02Z"));
  const initial = props();
  const final = { ...message, presentation: { ...presentation, final: true } };
  const { rerender } = render(<TurnProcess {...initial} message={final} />);
  expect(screen.getByText("工作中 2 秒")).toBeVisible();
  act(() => vi.advanceTimersByTime(3000));
  expect(screen.getByText("工作中 5 秒")).toBeVisible();
  rerender(<TurnProcess {...initial} message={{ ...final, streaming: false, durationMs: 5100 }} />);
  act(() => vi.advanceTimersByTime(10000));
  expect(screen.getByText("已工作 5.1 秒")).toBeVisible();
  rerender(<TurnProcess {...initial} message={{ ...final, streaming: false, presentation: undefined }} />);
  expect(screen.getByText("已工作")).toBeVisible();
});

it("历史默认折叠，停止和失败文案不冒充完成", () => {
  const initial = props();
  const { rerender } = render(<TurnProcess {...initial} message={{ ...message, streaming: false, status: "interrupted" }} />);
  expect(screen.getByRole("button", { name: "已停止" })).toHaveAttribute("aria-expanded", "false");
  rerender(<TurnProcess {...initial} message={{ ...message, streaming: false, status: "error" }} />);
  expect(screen.getByRole("button", { name: "工作未完成" })).toHaveAttribute("aria-expanded", "false");
});

it("未确认正文实时展示，后续工具到达后归入过程，最终正文不重复", () => {
  const draft = { ...presentation, parts: [...presentation.parts, { id: "answer", kind: "text" as const, text: "实时文字" }] };
  expect(messageProcess({ ...message, presentation: draft }).body).toBe("实时文字");
  expect(messageProcess({ ...message, presentation: draft, streaming: false, status: "interrupted" }).body).toBe("实时文字");
  expect(messageProcess({ ...message, presentation: { ...draft, parts: [...draft.parts, { id: "next", kind: "tool", call_id: "next" }] } }).body).toBe("");
  const final: TurnPresentation = { ...draft, final: true, parts: [...presentation.parts, { id: "answer", kind: "answer", text: "最终文字" }] };
  const result = messageProcess({ ...message, presentation: final });
  expect(result.body).toBe("最终文字");
  expect(result.parts.some((part) => part.kind === "answer")).toBe(false);
});

it("真实元数据可经过流式、完成和历史恢复，自动空检索不展示", () => {
  let state: ChatState = { ...initialChatState, sessionId: "web:test" };
  state = reduceChatFrame(state, { type: "turn.started", session_id: "web:test", turn_id: "turn" });
  state = reduceChatFrame(state, { type: "turn.presentation", session_id: "web:test", turn_id: "turn", presentation });
  state = reduceChatFrame(state, { type: "answer.delta", session_id: "web:test", turn_id: "turn", part_id: "answer", delta: "正文" });
  expect(messageProcess(state.messages.find((item) => item.role === "assistant")!).body).toBe("正文");
  const completed = { ...presentation, final: true };
  state = reduceChatFrame(state, { type: "message.final", session_id: "web:test", turn_id: "turn", content: "正文", metadata: { presentation: completed, duration_ms: 1234 } });
  expect(state.messages.find((item) => item.role === "assistant")?.presentation?.final).toBe(true);
  const [restored] = rowsToMessages([{ id: "saved", role: "assistant", content: "正文", metadata: { presentation: completed } }]);
  expect(restored.presentation).toEqual(completed);
  expect(readPresentation({ version: 1, parts: [{ id: "empty", kind: "memory", items: [] }] })?.parts).toEqual([]);
  expect(readPresentation({ version: 99, parts: [] })).toBeUndefined();
});
