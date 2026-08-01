import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { App } from "./App";

let systemDark = false;
let colorSchemeListener: ((event: MediaQueryListEvent) => void) | null = null;

class FakeWebSocket {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSED = 3;
  static instances: FakeWebSocket[] = [];
  readyState = FakeWebSocket.CONNECTING;
  sent: Array<Record<string, unknown>> = [];
  onopen: (() => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: (() => void) | null = null;

  constructor(_url: string) {
    FakeWebSocket.instances.push(this);
    queueMicrotask(() => { this.readyState = FakeWebSocket.OPEN; this.onopen?.(); });
  }

  send(raw: string) {
    const frame = JSON.parse(raw) as Record<string, unknown>;
    this.sent.push(frame);
    if (frame.type === "session.create") {
      queueMicrotask(() => this.onmessage?.({ data: JSON.stringify({ type: "session.created", request_id: frame.request_id, session_id: "web:component" }) } as MessageEvent));
    }
    if (frame.type === "message.send" && !frame.session_id) {
      queueMicrotask(() => this.onmessage?.({ data: JSON.stringify({ type: "session.created", request_id: frame.request_id, session_id: "web:component" }) } as MessageEvent));
    }
  }

  close() { this.readyState = FakeWebSocket.CLOSED; this.onclose?.(); }
}

beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
  window.history.replaceState({}, "", "/");
  document.documentElement.removeAttribute("data-theme");
  systemDark = false;
  colorSchemeListener = null;
  FakeWebSocket.instances = [];
  vi.stubGlobal("WebSocket", FakeWebSocket);
  vi.stubGlobal("ResizeObserver", class {
    observe() {}
    unobserve() {}
    disconnect() {}
  });
  vi.stubGlobal("IntersectionObserver", class {
    constructor(private readonly callback: IntersectionObserverCallback) {}
    observe(target: Element) {
      this.callback([{ isIntersecting: true, target } as IntersectionObserverEntry], this as unknown as IntersectionObserver);
    }
    unobserve() {}
    disconnect() {}
    takeRecords() { return []; }
    root = null;
    rootMargin = "0px";
    thresholds = [0];
  });
  Object.defineProperty(SVGElement.prototype, "getBBox", {
    configurable: true,
    value: () => ({ x: 0, y: 0, width: 120, height: 24 }),
  });
  Object.defineProperty(HTMLElement.prototype, "scrollTo", {
    configurable: true,
    value: vi.fn(),
  });
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: { writeText: vi.fn().mockResolvedValue(undefined) },
  });
  vi.spyOn(window, "open").mockImplementation(() => null);
  vi.stubGlobal("matchMedia", vi.fn(() => ({
    matches: systemDark,
    media: "(prefers-color-scheme: dark)",
    onchange: null,
    addEventListener: (_type: string, listener: (event: MediaQueryListEvent) => void) => { colorSchemeListener = listener; },
    removeEventListener: (_type: string, listener: (event: MediaQueryListEvent) => void) => {
      if (colorSchemeListener === listener) colorSchemeListener = null;
    },
    addListener: vi.fn(),
    removeListener: vi.fn(),
    dispatchEvent: vi.fn(),
  })));
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    const payload = url.includes("/messages") ? { items: [], total: 0 } : { items: [], total: 0 };
    return { ok: true, json: async () => payload } as Response;
  }));
  Object.defineProperty(URL, "createObjectURL", {
    configurable: true,
    value: vi.fn(() => "blob:preview"),
  });
  Object.defineProperty(URL, "revokeObjectURL", {
    configurable: true,
    value: vi.fn(),
  });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

it("首条消息发送后创建 Session 并保留用户消息", async () => {
  render(<App />);

  await screen.findByText("已连接");
  const socket = FakeWebSocket.instances[0];
  expect(socket.sent.some((frame) => frame.type === "session.create")).toBe(false);

  const input = screen.getByPlaceholderText("输入消息，或附加文本与图片");
  fireEvent.change(input, { target: { value: "组件测试消息" } });
  fireEvent.click(screen.getByRole("button", { name: "发送" }));

  await waitFor(() => expect(socket.sent.some((frame) => frame.type === "message.send" && frame.text === "组件测试消息")).toBe(true));
  const sent = socket.sent.find((frame) => frame.type === "message.send");
  expect(sent).not.toHaveProperty("session_id");
  await waitFor(() => expect(localStorage.getItem("beanagent.session_id")).toBe("web:component"));
  expect(window.location.pathname).toBe("/chat/component");
  expect(screen.getByText("组件测试消息", { selector: ".user-text" })).toBeVisible();
  expect(screen.getByRole("button", { name: "新对话" })).toBeVisible();
});

it("中断消息只显示状态标签，不渲染持久化占位正文", async () => {
  window.history.replaceState({}, "", "/chat/interrupted");
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes("/messages")) {
      return { ok: true, json: async () => ({ items: [
        {
          id: "web:interrupted:0",
          seq: 0,
          role: "user",
          content: "hello",
          status: "interrupted",
          turn_id: "turn-interrupted",
          timestamp: "2026-08-01T16:10:00+08:00",
        },
        {
          id: "web:interrupted:1",
          seq: 1,
          role: "assistant",
          content: "[用户已停止生成]",
          interrupted_display_content: "partial reply",
          interrupted_display_reasoning: "partial thinking",
          interrupted_thinking_status: "interrupted",
          tool_chain: [{ calls: [{
            call_id: "call-running", name: "shell", status: "interrupted", result: "",
          }] }],
          status: "interrupted",
          turn_id: "turn-interrupted",
          timestamp: "2026-08-01T16:10:01+08:00",
        },
      ] }) } as Response;
    }
    return { ok: true, json: async () => ({ items: [], total: 0 }) } as Response;
  }));

  render(<App />);

  expect(await screen.findByText("hello", { selector: ".user-text" })).toBeVisible();
  expect(screen.getByText("partial reply")).toBeVisible();
  const stoppedLabels = screen.getAllByText("已停止");
  expect(stoppedLabels).toHaveLength(2);
  fireEvent.click(stoppedLabels.find((label) => label.closest("button"))!.closest("button")!);
  expect(screen.getByText("partial thinking")).toBeVisible();
  expect(screen.queryByText("[用户已停止生成]")).not.toBeInTheDocument();
});

it("运行中新会话收到标题更新后替换侧栏占位", async () => {
  render(<App />);
  await screen.findByText("已连接");
  const socket = FakeWebSocket.instances[0];
  const input = screen.getByPlaceholderText("输入消息，或附加文本与图片");
  fireEvent.change(input, { target: { value: "标题问题" } });
  fireEvent.click(screen.getByRole("button", { name: "发送" }));
  await waitFor(() => expect(socket.sent.some((frame) => frame.type === "message.send")).toBe(true));
  await waitFor(() => expect(localStorage.getItem("beanagent.session_id")).toBe("web:component"));

  socket.onmessage?.({ data: JSON.stringify({
    type: "session.updated",
    session: {
      key: "web:component",
      title: "标题问题",
      first_message_content: "",
      message_count: 0,
      created_at: "2026-07-30T12:00:00+08:00",
      updated_at: "2026-07-30T12:00:00+08:00",
    },
  }) } as MessageEvent);

  await waitFor(() => expect(screen.getAllByRole("button", { name: "标题问题" }).length).toBeGreaterThanOrEqual(1));
});

it("刷新已有会话时显示骨架并通过消息接口恢复运行中 turn", async () => {
  localStorage.setItem("beanagent.session_id", "web:component");
  sessionStorage.setItem("beanagent.running_draft:web:component", JSON.stringify({
    version: 1,
    sessionId: "web:component",
    activeTurnId: "stale-turn",
    turnState: { status: "running", queuePosition: null, turnId: "stale-turn", requestId: "stale" },
    messages: [{ id: "stale-turn", role: "assistant", content: "stale cache", thinking: "", media: [], tools: [], turnId: "stale-turn", streaming: true }],
    savedAt: Date.now(),
  }));
  let resolveMessages: (response: Response) => void = () => {};
  const delayedMessages = new Promise<Response>((resolve) => { resolveMessages = resolve; });
  vi.mocked(fetch).mockImplementation(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes("/messages")) return delayedMessages;
    if (url.includes("/api/chat/sessions?page=")) {
      return { ok: true, json: async () => ({ items: [{
        key: "web:component",
        title: "restored title",
        first_message_content: "",
        message_count: 0,
        created_at: "2026-07-30T12:00:00+08:00",
        updated_at: "2026-07-30T12:00:00+08:00",
      }], total: 1 }) } as Response;
    }
    return { ok: true, json: async () => ({ items: [], total: 0 }) } as Response;
  });

  const { container } = render(<App />);

  expect(container.querySelector(".conversation-skeleton")).not.toBeNull();
  expect(screen.queryByText("stale cache")).not.toBeInTheDocument();
  expect(screen.queryByText("正在恢复会话")).not.toBeInTheDocument();
  expect(window.location.pathname).toBe("/chat/component");

  resolveMessages({ ok: true, json: async () => ({
    items: [
      { id: "running:user:turn-restored", role: "user", content: "fresh question", turn_id: "turn-restored", tool_chain: [], timestamp: "2026-07-30T12:00:00+08:00", metadata: { running: true } },
      { id: "running:assistant:turn-restored", role: "assistant", content: "fresh partial", reasoning_content: "fresh thinking", turn_id: "turn-restored", tool_chain: [{ calls: [{ call_id: "call-1", name: "read_file", status: "running", arguments: {}, result: "" }] }], timestamp: "2026-07-30T12:00:01+08:00", metadata: { running: true } },
    ],
    total: 0,
    has_more: false,
    next_before_seq: null,
  }) } as Response);

  expect(await screen.findByText("fresh question", { selector: ".user-text" })).toBeVisible();
  expect(screen.getByText("fresh partial")).toBeVisible();
  expect(screen.getByText("fresh thinking")).toBeVisible();
  expect(screen.getByText("read_file")).toBeVisible();
  expect(container.querySelector(".conversation-skeleton")).toBeNull();
  await waitFor(() => expect(FakeWebSocket.instances[0].sent.some((frame) => (
    frame.type === "session.subscribe" && frame.session_id === "web:component"
  ))).toBe(true));
  expect(vi.mocked(fetch).mock.calls.some(([input]) => String(input).endsWith("/api/chat/sessions/web%3Acomponent/messages"))).toBe(true);
});

it("点击未加载的全局轮次导航时按 seq 加载正文窗口", async () => {
  localStorage.setItem("beanagent.session_id", "web:component");
  let latestMessageRequests = 0;
  let resolveLatestMessageRequest!: (response: Response) => void;
  const latestMessageRequest = new Promise<Response>((resolve) => {
    resolveLatestMessageRequest = resolve;
  });
  vi.mocked(fetch).mockImplementation(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes("/api/chat/sessions?page=")) {
      return { ok: true, json: async () => ({ items: [{
        key: "web:component",
        title: "global turns",
        first_message_content: "",
        message_count: 122,
        created_at: "2026-07-30T12:00:00+08:00",
        updated_at: "2026-07-30T12:00:00+08:00",
      }], total: 1 }) } as Response;
    }
    if (url.endsWith("/api/chat/sessions/web%3Acomponent/turns")) {
      return { ok: true, json: async () => ({ items: [
        { id: "turn-old", seq: 40, turn_index: 21, question: "old question", preview: "old question", timestamp: "2026-07-30T10:00:00+08:00" },
        { id: "turn-new", seq: 120, turn_index: 61, question: "new question", preview: "new question", timestamp: "2026-07-30T12:00:00+08:00" },
      ] }) } as Response;
    }
    if (url.endsWith("/api/chat/sessions/web%3Acomponent/messages")) {
      latestMessageRequests += 1;
      if (latestMessageRequests === 3) {
        return latestMessageRequest;
      }
      return { ok: true, json: async () => ({
        items: [
          { id: "web:component:120", seq: 120, role: "user", content: "new question", turn_id: "turn-new", timestamp: "2026-07-30T12:00:00+08:00" },
          { id: "web:component:121", seq: 121, role: "assistant", content: "new answer", turn_id: "turn-new", timestamp: "2026-07-30T12:00:01+08:00" },
        ],
        has_more: true,
        next_before_seq: 120,
      }) } as Response;
    }
    if (url.includes("/api/chat/sessions/web%3Acomponent/messages/around")) {
      if (url.includes("anchor_seq=42")) {
        return { ok: true, json: async () => ({
          items: [
            { id: "web:component:42", seq: 42, role: "user", content: "next question", turn_id: "turn-next", timestamp: "2026-07-30T10:01:00+08:00" },
            { id: "web:component:43", seq: 43, role: "assistant", content: "next answer", turn_id: "turn-next", timestamp: "2026-07-30T10:01:01+08:00" },
          ],
          has_before: true,
          has_after: true,
          next_before_seq: 42,
        }) } as Response;
      }
      return { ok: true, json: async () => ({
        items: [
          { id: "web:component:40", seq: 40, role: "user", content: "old question", turn_id: "turn-old", timestamp: "2026-07-30T10:00:00+08:00" },
          { id: "web:component:41", seq: 41, role: "assistant", content: "old answer", turn_id: "turn-old", timestamp: "2026-07-30T10:00:01+08:00" },
        ],
        has_before: false,
        has_after: true,
        next_before_seq: null,
      }) } as Response;
    }
    if (url.includes("/notifications")) return { ok: true, json: async () => ({ items: [{
      id: "reminder-outside-window",
      content: "window outside reminder",
      source: "scheduled_reminder",
      source_id: "reminder-1",
      scheduled_at: "2026-07-30T09:00:00+08:00",
      generated_at: "2026-07-30T09:00:00+08:00",
      status: "delivered",
      recurring: false,
    }] }) } as Response;
    return { ok: true, json: async () => ({ items: [], total: 0 }) } as Response;
  });

  render(<App />);

  expect(await screen.findByText("new question", { selector: ".user-text" })).toBeVisible();
  expect(screen.getByTitle("old question")).toHaveTextContent("21");
  expect(screen.getByTitle("new question")).toHaveTextContent("61");

  fireEvent.click(screen.getByTitle("old question"));

  expect(await screen.findByRole("button", { name: "回到最新消息" })).toBeVisible();
  expect(await screen.findByText("old question", { selector: ".user-text" })).toBeVisible();
  expect(screen.getByText("old answer")).toBeVisible();
  expect(screen.queryByText("new question", { selector: ".user-text" })).not.toBeInTheDocument();
  await waitFor(() => expect(
    document.querySelector<HTMLElement>(".conversation-scroll")?.scrollTo,
  ).toHaveBeenCalled());
  expect(vi.mocked(fetch).mock.calls.some(([input]) => (
    String(input).endsWith("/api/chat/sessions/web%3Acomponent/messages/around?anchor_seq=20&limit=60")
  ))).toBe(true);

  fireEvent.click(screen.getByRole("button", { name: "回到最新消息" }));

  expect(await screen.findByText("new question", { selector: ".user-text" })).toBeVisible();
  expect(screen.queryByText("old question", { selector: ".user-text" })).not.toBeInTheDocument();
  expect(vi.mocked(fetch).mock.calls.filter(([input]) => (
    String(input).endsWith("/api/chat/sessions/web%3Acomponent/messages")
  ))).toHaveLength(2);

  fireEvent.click(screen.getByTitle("old question"));
  expect(await screen.findByText("old question", { selector: ".user-text" })).toBeVisible();

  const scroller = document.querySelector<HTMLElement>(".conversation-scroll")!;
  Object.defineProperties(scroller, {
    clientHeight: { configurable: true, value: 600 },
    scrollHeight: { configurable: true, value: 1200 },
    scrollTop: { configurable: true, writable: true, value: 600 },
  });
  fireEvent.scroll(scroller);

  expect(await screen.findByText("next question", { selector: ".user-text" })).toBeVisible();
  expect(screen.getByRole("button", { name: "回到最新消息" })).toBeVisible();
  expect(vi.mocked(fetch).mock.calls.some(([input]) => (
    String(input).endsWith("/api/chat/sessions/web%3Acomponent/messages/around?anchor_seq=42&limit=60")
  ))).toBe(true);

  const input = screen.getByPlaceholderText("输入消息，或附加文本与图片");
  fireEvent.change(input, { target: { value: "send from history window" } });
  fireEvent.click(screen.getByRole("button", { name: "发送" }));

  await waitFor(() => expect(FakeWebSocket.instances[0].sent.some((frame) => (
    frame.type === "message.send" && frame.text === "send from history window"
  ))).toBe(true));
  const sent = FakeWebSocket.instances[0].sent.find((frame) => (
    frame.type === "message.send" && frame.text === "send from history window"
  ));
  await act(async () => {
    FakeWebSocket.instances[0].onmessage?.({ data: JSON.stringify({
      type: "turn.started",
      request_id: sent?.request_id,
      session_id: "web:component",
      turn_id: "turn-sent",
    }) } as MessageEvent);
    resolveLatestMessageRequest({ ok: true, json: async () => ({
      items: [
        { id: "web:component:120", seq: 120, role: "user", content: "new question", turn_id: "turn-new", timestamp: "2026-07-30T12:00:00+08:00" },
        { id: "web:component:121", seq: 121, role: "assistant", content: "new answer", turn_id: "turn-new", timestamp: "2026-07-30T12:00:01+08:00" },
      ],
      has_more: true,
      next_before_seq: 120,
    }) } as Response);
    await latestMessageRequest;
  });
  expect(await screen.findByText("send from history window", { selector: ".user-text" })).toBeVisible();
  expect(screen.getAllByText("send from history window", { selector: ".user-text" })).toHaveLength(1);
  expect(screen.queryByText("old question", { selector: ".user-text" })).not.toBeInTheDocument();
  expect(vi.mocked(fetch).mock.calls.filter(([request]) => (
    String(request).endsWith("/api/chat/sessions/web%3Acomponent/messages")
  ))).toHaveLength(3);
});

it("滚动到正文窗口顶部时继续加载更早消息", async () => {
  localStorage.setItem("beanagent.session_id", "web:component");
  vi.mocked(fetch).mockImplementation(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes("/api/chat/sessions?page=")) {
      return { ok: true, json: async () => ({ items: [{
        key: "web:component",
        title: "paged turns",
        first_message_content: "",
        message_count: 64,
        created_at: "2026-07-30T12:00:00+08:00",
        updated_at: "2026-07-30T12:00:00+08:00",
      }], total: 1 }) } as Response;
    }
    if (url.endsWith("/api/chat/sessions/web%3Acomponent/turns")) {
      return { ok: true, json: async () => ({ items: [
        { id: "turn-old", seq: 58, turn_index: 30, question: "older question", preview: "older question", timestamp: "2026-07-30T10:00:00+08:00" },
        { id: "turn-current", seq: 60, turn_index: 31, question: "current question", preview: "current question", timestamp: "2026-07-30T12:00:00+08:00" },
      ] }) } as Response;
    }
    if (url.endsWith("/api/chat/sessions/web%3Acomponent/messages")) {
      return { ok: true, json: async () => ({
        items: [
          { id: "web:component:60", seq: 60, role: "user", content: "current question", turn_id: "turn-current", timestamp: "2026-07-30T12:00:00+08:00" },
        ],
        has_more: true,
        next_before_seq: 60,
      }) } as Response;
    }
    if (url.includes("/api/chat/sessions/web%3Acomponent/messages/older")) {
      if (url.includes("before_seq=58")) {
        return { ok: true, json: async () => ({
          items: [
            { id: "web:component:56", seq: 56, role: "user", content: "oldest question", turn_id: "turn-oldest", timestamp: "2026-07-30T09:00:00+08:00" },
            { id: "web:component:57", seq: 57, role: "assistant", content: "oldest answer", turn_id: "turn-oldest", timestamp: "2026-07-30T09:00:01+08:00" },
          ],
          has_more: false,
          next_before_seq: null,
        }) } as Response;
      }
      return { ok: true, json: async () => ({
        items: [
          { id: "web:component:58", seq: 58, role: "user", content: "older question", turn_id: "turn-old", timestamp: "2026-07-30T10:00:00+08:00" },
          { id: "web:component:59", seq: 59, role: "assistant", content: "older answer", turn_id: "turn-old", timestamp: "2026-07-30T10:00:01+08:00" },
        ],
        has_more: true,
        next_before_seq: 58,
      }) } as Response;
    }
    if (url.includes("/notifications")) return { ok: true, json: async () => ({ items: [{
      id: "reminder-outside-window",
      content: "window outside reminder",
      source: "scheduled_reminder",
      source_id: "reminder-1",
      scheduled_at: "2026-07-30T09:00:00+08:00",
      generated_at: "2026-07-30T09:00:00+08:00",
      status: "delivered",
      recurring: false,
    }] }) } as Response;
    return { ok: true, json: async () => ({ items: [], total: 0 }) } as Response;
  });

  render(<App />);

  expect(await screen.findByText("current question", { selector: ".user-text" })).toBeVisible();
  expect(screen.queryByText("window outside reminder")).not.toBeInTheDocument();
  const scroller = document.querySelector<HTMLElement>(".conversation-scroll")!;
  Object.defineProperties(scroller, {
    clientHeight: { configurable: true, value: 600 },
    scrollHeight: {
      configurable: true,
      get: () => screen.queryByText("older question", { selector: ".user-text" }) ? 2400 : 1200,
    },
    scrollTop: { configurable: true, writable: true, value: 0 },
  });
  vi.mocked(scroller.scrollTo).mockClear();
  fireEvent.scroll(scroller);

  expect(await screen.findByText("older question", { selector: ".user-text" })).toBeVisible();
  expect(screen.getByText("older answer")).toBeVisible();
  await act(async () => {
    await new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve())));
  });
  expect(vi.mocked(scroller.scrollTo).mock.calls.some(([options]) => (
    typeof options === "object" && options !== null && Number((options as ScrollToOptions).top) > 0
  ))).toBe(true);

  await act(async () => { await new Promise<void>((resolve) => setTimeout(resolve, 0)); });
  scroller.scrollTop = 0;
  vi.mocked(scroller.scrollTo).mockClear();
  fireEvent.scroll(scroller);

  expect(await screen.findByText("oldest question", { selector: ".user-text" })).toBeVisible();
  expect(vi.mocked(scroller.scrollTo).mock.calls.some(([options]) => (
    typeof options === "object" && options !== null && Number((options as ScrollToOptions).top) > 0
  ))).toBe(true);
  expect(screen.getByText("window outside reminder")).toBeVisible();
  expect(vi.mocked(fetch).mock.calls.some(([input]) => (
    String(input).endsWith("/api/chat/sessions/web%3Acomponent/messages/older?before_seq=60&limit=60")
  ))).toBe(true);
});

it("将晚于最新正文窗口的提醒插入末尾", async () => {
  localStorage.setItem("beanagent.session_id", "web:component");
  vi.mocked(fetch).mockImplementation(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes("/api/chat/sessions?page=")) {
      return { ok: true, json: async () => ({ items: [{
        key: "web:component",
        title: "notification after window",
        first_message_content: "",
        message_count: 2,
        created_at: "2026-07-30T23:00:00+08:00",
        updated_at: "2026-07-30T23:56:00+08:00",
      }], total: 1 }) } as Response;
    }
    if (url.endsWith("/api/chat/sessions/web%3Acomponent/turns")) {
      return { ok: true, json: async () => ({ items: [] }) } as Response;
    }
    if (url.endsWith("/api/chat/sessions/web%3Acomponent/messages")) {
      return { ok: true, json: async () => ({
        items: [
          { id: "web:component:291", seq: 291, role: "user", content: "晚安", turn_id: "turn-last", timestamp: "2026-07-30T23:56:00+08:00" },
          { id: "web:component:292", seq: 292, role: "assistant", content: "明天见", turn_id: "turn-last", timestamp: "2026-07-30T23:56:01+08:00" },
        ],
        has_more: false,
        next_before_seq: null,
      }) } as Response;
    }
    if (url.includes("/notifications")) return { ok: true, json: async () => ({ items: [{
      id: "morning-news",
      content: "今日 AI 圈早报",
      source: "scheduled_soft",
      source_id: "job-news",
      scheduled_at: "2026-07-31T10:00:00+08:00",
      generated_at: "2026-07-31T10:03:41+08:00",
      status: "delivered",
      recurring: true,
    }] }) } as Response;
    return { ok: true, json: async () => ({ items: [], total: 0 }) } as Response;
  });

  render(<App />);

  expect(await screen.findByText("晚安", { selector: ".user-text" })).toBeVisible();
  expect(screen.getByText("明天见")).toBeVisible();
  expect(screen.getByText("今日 AI 圈早报")).toBeVisible();
});

it("非连续正文窗口只插入已加载区间内的提醒", async () => {
  localStorage.setItem("beanagent.session_id", "web:component");
  vi.mocked(fetch).mockImplementation(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes("/api/chat/sessions?page=")) {
      return { ok: true, json: async () => ({ items: [{
        key: "web:component",
        title: "split windows",
        first_message_content: "",
        message_count: 122,
        created_at: "2026-07-30T08:00:00+08:00",
        updated_at: "2026-07-30T12:00:00+08:00",
      }], total: 1 }) } as Response;
    }
    if (url.endsWith("/api/chat/sessions/web%3Acomponent/turns")) {
      return { ok: true, json: async () => ({ items: [
        { id: "turn-old", seq: 0, turn_index: 1, question: "old anchor", preview: "old anchor", timestamp: "2026-07-30T08:00:00+08:00" },
        { id: "turn-new", seq: 120, turn_index: 61, question: "new anchor", preview: "new anchor", timestamp: "2026-07-30T12:00:00+08:00" },
      ] }) } as Response;
    }
    if (url.endsWith("/api/chat/sessions/web%3Acomponent/messages")) {
      return { ok: true, json: async () => ({
        items: [
          { id: "web:component:120", seq: 120, role: "user", content: "new anchor", turn_id: "turn-new", timestamp: "2026-07-30T12:00:00+08:00" },
          { id: "web:component:121", seq: 121, role: "assistant", content: "new answer", turn_id: "turn-new", timestamp: "2026-07-30T12:00:01+08:00" },
        ],
        has_more: true,
        next_before_seq: 120,
      }) } as Response;
    }
    if (url.includes("/api/chat/sessions/web%3Acomponent/messages/around")) {
      return { ok: true, json: async () => ({
        items: [
          { id: "web:component:0", seq: 0, role: "user", content: "old anchor", turn_id: "turn-old", timestamp: "2026-07-30T08:00:00+08:00" },
          { id: "web:component:1", seq: 1, role: "assistant", content: "old answer", turn_id: "turn-old", timestamp: "2026-07-30T08:00:01+08:00" },
        ],
        has_before: false,
        has_after: true,
        next_before_seq: null,
      }) } as Response;
    }
    if (url.includes("/notifications")) return { ok: true, json: async () => ({ items: [
      {
        id: "old-window-notice",
        content: "旧窗口提醒",
        source: "scheduled_reminder",
        source_id: "job-old",
        scheduled_at: "2026-07-30T08:00:00+08:00",
        generated_at: "2026-07-30T08:00:00+08:00",
        status: "delivered",
        recurring: false,
      },
      {
        id: "gap-notice",
        content: "中间未加载提醒",
        source: "scheduled_reminder",
        source_id: "job-gap",
        scheduled_at: "2026-07-30T10:00:00+08:00",
        generated_at: "2026-07-30T10:00:00+08:00",
        status: "delivered",
        recurring: false,
      },
      {
        id: "tail-notice",
        content: "尾部之后提醒",
        source: "scheduled_soft",
        source_id: "job-tail",
        scheduled_at: "2026-07-30T12:30:00+08:00",
        generated_at: "2026-07-30T12:30:00+08:00",
        status: "delivered",
        recurring: true,
      },
    ] }) } as Response;
    return { ok: true, json: async () => ({ items: [], total: 0 }) } as Response;
  });

  render(<App />);

  expect(await screen.findByText("new anchor", { selector: ".user-text" })).toBeVisible();
  expect(screen.getByText("尾部之后提醒")).toBeVisible();
  expect(screen.queryByText("中间未加载提醒")).not.toBeInTheDocument();

  fireEvent.click(screen.getByTitle("old anchor"));

  expect(await screen.findByText("old anchor", { selector: ".user-text" })).toBeVisible();
  expect(screen.getByText("旧窗口提醒")).toBeVisible();
  expect(screen.queryByText("尾部之后提醒")).not.toBeInTheDocument();
  expect(screen.queryByText("中间未加载提醒")).not.toBeInTheDocument();
});

it("新建会话会清空其他会话中尚未发送的输入", async () => {
  render(<App />);
  await screen.findByText("已连接");
  const input = screen.getByPlaceholderText("输入消息，或附加文本与图片");
  fireEvent.change(input, { target: { value: "不应带到新会话" } });
  const attachment = new File(["draft"], "draft.txt", { type: "text/plain" });
  const fileInput = document.querySelector<HTMLInputElement>('input[type="file"]')!;
  fireEvent.change(fileInput, { target: { files: [attachment] } });
  expect(await screen.findByText("draft.txt")).toBeVisible();

  fireEvent.click(screen.getByRole("button", { name: "新建会话" }));

  expect(input).toHaveValue("");
  expect(screen.queryByText("draft.txt")).not.toBeInTheDocument();
  expect(window.location.pathname).toBe("/");
});

it("延迟返回的旧会话列表不会移除正在排队的新会话", async () => {
  let resolveSessions!: (response: Response) => void;
  const delayedSessions = new Promise<Response>((resolve) => { resolveSessions = resolve; });
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    if (String(input).includes("/api/chat/sessions?page=")) return delayedSessions;
    return { ok: true, json: async () => ({ items: [], total: 0 }) } as Response;
  }));
  render(<App />);

  await screen.findByText("已连接");
  const input = screen.getByPlaceholderText("输入消息，或附加文本与图片");
  fireEvent.change(input, { target: { value: "等待队列的消息" } });
  fireEvent.click(screen.getByRole("button", { name: "发送" }));
  expect(await screen.findByRole("button", { name: "新对话" })).toBeVisible();

  await act(async () => {
    resolveSessions({ ok: true, json: async () => ({ items: [], total: 0 }) } as Response);
    await delayedSessions;
  });
  expect(screen.getByRole("button", { name: "新对话" })).toBeVisible();
});

it("排队时展示动态位置并允许停止取消", async () => {
  render(<App />);
  await screen.findByText("已连接");
  const socket = FakeWebSocket.instances[0];
  const input = screen.getByPlaceholderText("输入消息，或附加文本与图片");
  fireEvent.change(input, { target: { value: "需要排队的问题" } });
  fireEvent.click(screen.getByRole("button", { name: "发送" }));
  await waitFor(() => expect(socket.sent.some((frame) => frame.type === "message.send")).toBe(true));
  const sent = socket.sent.find((frame) => frame.type === "message.send");

  socket.onmessage?.({ data: JSON.stringify({
    type: "turn.queued",
    request_id: sent?.request_id,
    session_id: "web:component",
    position: 1,
  }) } as MessageEvent);
  expect(await screen.findByText("排队中 · 即将开始")).toBeVisible();
  expect(screen.getByRole("button", { name: "停止" })).toBeVisible();

  socket.onmessage?.({ data: JSON.stringify({
    type: "turn.queued",
    request_id: sent?.request_id,
    session_id: "web:component",
    position: 2,
  }) } as MessageEvent);
  expect(await screen.findByText("排队中 · 前面还有 1 个会话")).toBeVisible();

  fireEvent.click(screen.getByRole("button", { name: "停止" }));
  expect(socket.sent.at(-1)).toMatchObject({ type: "turn.stop", session_id: "web:component" });
});

it("切回后台运行会话时恢复用户问题流式内容和工具状态", async () => {
  let resolveComponentHistory!: (response: Response) => void;
  const delayedComponentHistory = new Promise<Response>((resolve) => { resolveComponentHistory = resolve; });
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes("web%3Acomponent/messages")) return delayedComponentHistory;
    const payload = url.includes("/messages") || url.includes("/notifications")
      ? { items: [], total: 0 }
      : {
          items: [{
            key: "web:other", title: "其他会话", first_message_content: "其他会话",
            created_at: new Date().toISOString(), updated_at: new Date().toISOString(), message_count: 2,
          }],
          total: 1,
        };
    return { ok: true, json: async () => payload } as Response;
  }));
  render(<App />);
  await screen.findByText("已连接");
  const socket = FakeWebSocket.instances[0];
  const input = screen.getByPlaceholderText("输入消息，或附加文本与图片");
  fireEvent.change(input, { target: { value: "分析当前项目" } });
  fireEvent.click(screen.getByRole("button", { name: "发送" }));
  await waitFor(() => expect(socket.sent.some((frame) => frame.type === "message.send")).toBe(true));
  const sent = socket.sent.find((frame) => frame.type === "message.send");
  await act(async () => {
    resolveComponentHistory({ ok: true, json: async () => ({ items: [], total: 0 }) } as Response);
    await delayedComponentHistory;
  });
  socket.onmessage?.({ data: JSON.stringify({
    type: "turn.queued", request_id: sent?.request_id, session_id: "web:component", position: 1,
  }) } as MessageEvent);
  fireEvent.click(await screen.findByRole("button", { name: "其他会话" }));
  await waitFor(() => expect(localStorage.getItem("beanagent.session_id")).toBe("web:other"));

  socket.onmessage?.({ data: JSON.stringify({
    type: "turn.started", request_id: sent?.request_id, session_id: "web:component", turn_id: "turn-background",
  }) } as MessageEvent);
  socket.onmessage?.({ data: JSON.stringify({
    type: "answer.delta", session_id: "web:component", turn_id: "turn-background", delta: "阶段结果",
  }) } as MessageEvent);
  socket.onmessage?.({ data: JSON.stringify({
    type: "react.tool.started", session_id: "web:component", turn_id: "turn-background",
    call_id: "call-1", tool_name: "list_dir", arguments: { path: "." },
  }) } as MessageEvent);
  socket.onmessage?.({ data: JSON.stringify({
    type: "react.tool.completed", session_id: "web:component", turn_id: "turn-background",
    call_id: "call-1", tool_name: "list_dir", status: "ok", result_preview: "agent, tests",
  }) } as MessageEvent);

  fireEvent.click(screen.getByRole("button", { name: "新对话" }));
  expect(await screen.findByText("分析当前项目", { selector: ".user-text" })).toBeVisible();
  expect(screen.getByText("阶段结果")).toBeVisible();
  expect(screen.getByText("list_dir")).toBeVisible();
  expect(screen.queryByText("排队中 · 即将开始")).not.toBeInTheDocument();
});

it("会话列表使用最近更新时间分组", async () => {
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const payload = String(input).includes("/messages")
      ? { items: [], total: 0 }
      : {
          items: [{
            key: "web:first-time",
            first_message_content: "固定创建时间",
            created_at: "2026-01-02T10:00:00+08:00",
            updated_at: "2026-06-10T10:00:00+08:00",
          }],
          total: 1,
        };
    return { ok: true, json: async () => payload } as Response;
  }));

  const { container } = render(<App />);

  await screen.findByText("固定创建时间");
  expect(screen.getByText("2026-06", { selector: ".session-group-title" })).toBeVisible();
  expect(screen.queryByText("2026-01", { selector: ".session-group-title" })).not.toBeInTheDocument();
  expect(container.querySelector(".session-row time")).toBeNull();
});

it("会话列表显示时间分组标题", async () => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date("2026-07-19T18:00:00+08:00"));
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const payload = String(input).includes("/messages") ? { items: [], total: 0 } : {
      items: [
        { key: "web:today", first_message_content: "今天会话", created_at: "2026-07-19T10:00:00+08:00", updated_at: "2026-07-19T10:00:00+08:00" },
        { key: "web:yesterday", first_message_content: "昨天会话", created_at: "2026-07-18T10:00:00+08:00", updated_at: "2026-07-18T10:00:00+08:00" },
      ],
      total: 2,
    };
    return { ok: true, json: async () => payload } as Response;
  }));

  render(<App />);

  expect(await screen.findByText("今天", { selector: ".session-group-title" })).toBeVisible();
  expect(screen.getByText("昨天", { selector: ".session-group-title" })).toBeVisible();
  vi.useRealTimers();
});

it("通过会话菜单重命名且不触发会话切换", async () => {
  window.history.replaceState({}, "", "/chat/rename");
  let renamed = false;
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (init?.method === "PATCH") {
      renamed = true;
      return { ok: true, json: async () => ({ key: "web:rename", title: "新标题" }) } as Response;
    }
    const payload = url.includes("/messages") ? { items: [], total: 0 } : {
      items: [{ key: "web:rename", first_message_content: "原始标题", title: renamed ? "新标题" : "", created_at: new Date().toISOString(), updated_at: new Date().toISOString() }],
      total: 1,
    };
    return { ok: true, json: async () => payload } as Response;
  }));

  render(<App />);
  // 等待侧栏会话标题出现（顶栏可能也显示同名标题）
  const sidebarTitle = await screen.findByText("原始标题", { selector: ".session-row-select span" });
  expect(sidebarTitle).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: /打开会话.*原始标题.*的菜单/ }));
  fireEvent.click(screen.getByRole("menuitem", { name: "重命名" }));
  const input = screen.getByRole("textbox", { name: "会话标题" });
  fireEvent.change(input, { target: { value: "新标题" } });
  fireEvent.keyDown(input, { key: "Enter" });

  // 重命名后侧栏和顶栏都会显示新标题
  const newTitleElements = await screen.findAllByText("新标题");
  expect(newTitleElements.length).toBeGreaterThanOrEqual(1);
  expect(fetch).toHaveBeenCalledWith(
    "/api/chat/sessions/web%3Arename",
    expect.objectContaining({ method: "PATCH" }),
  );
});

it("点击会话菜单外部会关闭菜单", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => ({
    ok: true,
    json: async () => ({
      items: [{ key: "web:menu", first_message_content: "菜单会话", created_at: new Date().toISOString(), updated_at: new Date().toISOString() }],
    }),
  }) as Response));

  render(<App />);
  await screen.findByText("菜单会话");
  fireEvent.click(screen.getByRole("button", { name: "打开会话“菜单会话”的菜单" }));
  expect(screen.getByRole("menu")).toBeVisible();
  fireEvent.pointerDown(screen.getByRole("main"));
  expect(screen.queryByRole("menu")).not.toBeInTheDocument();
});

it("重命名失焦后保存并收起输入框", async () => {
  let renamed = false;
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    if (init?.method === "PATCH") {
      renamed = true;
      return { ok: true, json: async () => ({ key: "web:blur", title: "失焦标题" }) } as Response;
    }
    const payload = String(input).includes("/messages") ? { items: [] } : {
      items: [{ key: "web:blur", first_message_content: "原始会话", title: renamed ? "失焦标题" : "", created_at: new Date().toISOString(), updated_at: new Date().toISOString() }],
    };
    return { ok: true, json: async () => payload } as Response;
  }));

  render(<App />);
  await screen.findByText("原始会话");
  fireEvent.click(screen.getByRole("button", { name: "打开会话“原始会话”的菜单" }));
  fireEvent.click(screen.getByRole("menuitem", { name: "重命名" }));
  fireEvent.change(screen.getByRole("textbox", { name: "会话标题" }), { target: { value: "失焦标题" } });
  fireEvent.blur(screen.getByRole("textbox", { name: "会话标题" }));

  expect(await screen.findByText("失焦标题")).toBeVisible();
  expect(screen.queryByRole("textbox", { name: "会话标题" })).not.toBeInTheDocument();
});

it("会话列表不重复展示日期且聊天输入框始终存在", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => ({
    ok: true,
    json: async () => ({
      items: [{ key: "web:no-date", first_message_content: "无日期会话", created_at: new Date().toISOString(), updated_at: new Date().toISOString() }],
    }),
  }) as Response));

  const { container } = render(<App />);
  await screen.findByText("无日期会话");
  expect(container.querySelector(".session-row time")).toBeNull();
  expect(screen.getByPlaceholderText("输入消息，或附加文本与图片")).toBeVisible();
});

it("点击删除确认框外部会关闭确认框", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => ({
    ok: true,
    json: async () => ({
      items: [{ key: "web:dialog", first_message_content: "删除弹窗", created_at: new Date().toISOString(), updated_at: new Date().toISOString() }],
    }),
  }) as Response));

  const { container } = render(<App />);
  await screen.findByText("删除弹窗");
  fireEvent.click(screen.getByRole("button", { name: "打开会话“删除弹窗”的菜单" }));
  fireEvent.click(screen.getByRole("menuitem", { name: "删除" }));
  expect(screen.getByRole("dialog")).toBeVisible();
  const overlay = document.querySelector<HTMLElement>(".dialog-overlay");
  expect(overlay).not.toBeNull();
  fireEvent.pointerDown(overlay!);
  fireEvent.click(overlay!);
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(container.querySelector(".composer textarea")).toBeVisible();
});

it("删除当前会话后复用新建流程并回到空白会话", async () => {
  window.history.replaceState({}, "", "/chat/delete");
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (init?.method === "DELETE") return { ok: true, status: 204 } as Response;
    const payload = url.includes("web%3Adelete/messages") ? {
      items: [{ id: "web:delete:0", role: "user", content: "即将删除", tool_chain: [] }],
    } : url.includes("/messages") ? { items: [] } : {
      items: [{ key: "web:delete", first_message_content: "即将删除", created_at: new Date().toISOString(), updated_at: new Date().toISOString() }],
    };
    return { ok: true, json: async () => payload } as Response;
  }));

  render(<App />);
  await screen.findAllByText("即将删除");
  fireEvent.click(screen.getByRole("button", { name: "打开会话“即将删除”的菜单" }));
  fireEvent.click(screen.getByRole("menuitem", { name: "删除" }));
  expect(screen.getByRole("dialog")).toHaveTextContent("已沉淀的长期记忆不会随会话删除。");
  fireEvent.click(screen.getByRole("button", { name: "确认删除" }));

  await waitFor(() => expect(fetch).toHaveBeenCalledWith(
    "/api/chat/sessions/web%3Adelete",
    expect.objectContaining({ method: "DELETE" }),
  ));
  const socket = FakeWebSocket.instances[0];
  expect(socket.sent.filter((frame) => frame.type === "session.create")).toHaveLength(0);
  expect(localStorage.getItem("beanagent.session_id")).toBeNull();
  expect(window.location.pathname).toBe("/");
  expect(screen.queryAllByText("即将删除")).toHaveLength(0);
});

it("用户向上滚动后流式增量不抢回视口并可主动回到底部", async () => {
  window.history.replaceState({}, "", "/chat/scroll-lock");
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const payload = String(input).includes("/messages") ? {
      items: [{
        id: "web:scroll-lock:0",
        role: "assistant",
        content: "已有的长回答",
        turn_id: "old-turn",
        tool_chain: [],
        timestamp: "2026-07-18T10:00:00+08:00",
      }],
      total: 1,
    } : { items: [], total: 0 };
    return { ok: true, json: async () => payload } as Response;
  }));

  const { container } = render(<App />);
  await screen.findByText("已有的长回答");
  const conversation = container.querySelector<HTMLElement>(".conversation-scroll");
  expect(conversation).not.toBeNull();
  Object.defineProperties(conversation!, {
    scrollHeight: { configurable: true, value: 1200 },
    clientHeight: { configurable: true, value: 400 },
    scrollTop: { configurable: true, writable: true, value: 300 },
  });
  conversation!.style.overflow = "auto";
  fireEvent.wheel(conversation!, { deltaY: -120 });
  const latestButton = await screen.findByRole("button", { name: "回到最新消息" });
  const socket = FakeWebSocket.instances[0];
  socket.onmessage?.({ data: JSON.stringify({
    type: "turn.started", request_id: "r-scroll", session_id: "web:scroll-lock",
    turn_id: "turn-scroll", content: "继续",
  }) } as MessageEvent);
  socket.onmessage?.({ data: JSON.stringify({
    type: "answer.delta", session_id: "web:scroll-lock", turn_id: "turn-scroll", delta: "新内容",
  }) } as MessageEvent);

  await screen.findByText("新内容");
  expect(conversation!.scrollTop).toBe(300);
  fireEvent.click(latestButton);
  await waitFor(() => expect(conversation!.scrollTop).toBeGreaterThan(798));
  expect(screen.queryByRole("button", { name: "回到最新消息" })).not.toBeInTheDocument();
});

it("点击历史会话后在目标消息渲染完成时强制回到底部", async () => {
  window.history.replaceState({}, "", "/chat/first");
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    const payload = url.includes("/messages") ? {
      items: [{
        id: url.includes("web%3Asecond") ? "web:second:0" : "web:first:0",
        role: "assistant",
        content: url.includes("web%3Asecond") ? "第二个会话的末尾" : "第一个会话的末尾",
        turn_id: url.includes("web%3Asecond") ? "turn-second" : "turn-first",
        tool_chain: [],
        timestamp: "2026-07-18T10:00:00+08:00",
      }],
      total: 1,
    } : {
      items: [
        { key: "web:first", first_message_content: "第一个会话", updated_at: "2026-07-18T10:00:00+08:00" },
        { key: "web:second", first_message_content: "第二个会话", updated_at: "2026-07-18T11:00:00+08:00" },
      ],
      total: 2,
    };
    return { ok: true, json: async () => payload } as Response;
  }));

  const { container } = render(<App />);
  await screen.findByText("第一个会话的末尾");
  expect(screen.getByRole("log")).toHaveClass("conversation");
  const conversation = container.querySelector<HTMLElement>(".conversation-scroll");
  expect(conversation).not.toBeNull();
  Object.defineProperties(conversation, {
    scrollHeight: { configurable: true, value: 1600 },
    clientHeight: { configurable: true, value: 400 },
    scrollTop: { configurable: true, writable: true, value: 250 },
  });
  const firstVirtualConversation = container.querySelector(".virtual-conversation");
  fireEvent.click(screen.getByRole("button", { name: "第二个会话" }));

  await screen.findByText("第二个会话的末尾");
  expect(container.querySelector(".virtual-conversation")).not.toBe(firstVirtualConversation);
  await waitFor(() => expect(conversation!.scrollTop).toBeGreaterThanOrEqual(1197));
  expect(container.querySelector(".conversation-content")).not.toBeNull();
});

it("主题支持浅色、跟随系统和深色并持久化选择", async () => {
  render(<App />);
  await screen.findByText("已连接");

  const system = screen.getByRole("button", { name: "跟随系统" });
  const dark = screen.getByRole("button", { name: "深色" });
  const light = screen.getByRole("button", { name: "浅色" });
  expect(system).toHaveAttribute("aria-pressed", "true");
  expect(document.documentElement).toHaveAttribute("data-theme", "light");

  fireEvent.click(dark);
  expect(document.documentElement).toHaveAttribute("data-theme", "dark");
  expect(localStorage.getItem("beanagent.theme")).toBe("dark");
  expect(dark).toHaveAttribute("aria-pressed", "true");

  fireEvent.click(light);
  expect(document.documentElement).toHaveAttribute("data-theme", "light");
  expect(localStorage.getItem("beanagent.theme")).toBe("light");

  fireEvent.click(system);
  systemDark = true;
  colorSchemeListener?.({ matches: true } as MediaQueryListEvent);
  await waitFor(() => expect(document.documentElement).toHaveAttribute("data-theme", "dark"));
  expect(localStorage.getItem("beanagent.theme")).toBe("system");
});

it("粗体包裹的裸链接不显示星号并可直接跳转", async () => {
  window.history.replaceState({}, "", "/chat/links");
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    const payload = url.includes("/messages") ? {
      items: [{
        id: "web:links:0",
        role: "assistant",
        content: "记下了，你的 GitHub 地址是 **https://github.com/longbean1016/**。",
        turn_id: "turn-links",
        tool_chain: [],
        timestamp: "2026-07-16T20:00:00+08:00",
      }],
      total: 1,
    } : { items: [], total: 0 };
    return { ok: true, json: async () => payload } as Response;
  }));

  render(<App />);

  const link = await screen.findByRole("link", { name: "https://github.com/longbean1016/" });
  expect(link).toHaveTextContent("https://github.com/longbean1016/");
  expect(screen.queryByText(/longbean1016\/\*\*/)).not.toBeInTheDocument();
  expect(link).toHaveAttribute("href", "https://github.com/longbean1016/");
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
});

it("将记忆引用渲染为编号并支持展开和复制ID", async () => {
  window.history.replaceState({}, "", "/chat/citations");
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const payload = String(input).includes("/messages") ? {
      items: [{
        id: "web:citations:0",
        role: "assistant",
        content: "记得你的电脑型号。§cited:[memory_1,memory-2]§",
        turn_id: "turn-citations",
        tool_chain: [],
        timestamp: "2026-07-19T18:00:00+08:00",
      }],
      total: 1,
    } : { items: [], total: 0 };
    return { ok: true, json: async () => payload } as Response;
  }));

  const { container } = render(<App />);

  await screen.findByText("记得你的电脑型号。", { exact: false });
  expect(screen.queryByRole("link", { name: "[1]" })).not.toBeInTheDocument();
  expect(screen.getAllByText("[1]").length).toBeGreaterThan(0);
  expect(container.querySelector(".memory-citation-inline")).toHaveTextContent("[1]");
  expect(screen.queryByText(/§cited/)).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "查看引用 1" }));
  expect(screen.getAllByText("记忆 ID：")).toHaveLength(2);
  expect(screen.getByText("memory_1")).toBeVisible();
  expect(screen.getByText("memory-2")).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "复制记忆ID memory_1" }));
  expect(navigator.clipboard.writeText).toHaveBeenCalledWith("memory_1");
});

it("代码复制保留原始格式并将按钮图标切换为勾选", async () => {
  window.history.replaceState({}, "", "/chat/code-copy");
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const payload = String(input).includes("/messages") ? {
      items: [{
        id: "web:code-copy:0",
        role: "assistant",
        content: "```ts\nfunction demo() {\n  const ready = true;\n\n  return ready;\n}\n```",
        turn_id: "turn-code",
        tool_chain: [],
        timestamp: "2026-07-16T20:00:00+08:00",
      }],
      total: 1,
    } : { items: [], total: 0 };
    return { ok: true, json: async () => payload } as Response;
  }));

  render(<App />);

  const copy = await screen.findByRole("button", { name: "复制代码" });
  const copyIcon = copy.innerHTML;
  fireEvent.click(copy);
  await waitFor(() => expect(copy.innerHTML).not.toBe(copyIcon));
  expect(navigator.clipboard.writeText).toHaveBeenCalledWith(
    "function demo() {\n  const ready = true;\n\n  return ready;\n}\n",
  );
});

it("Mermaid fenced block 生成受限图表而不是普通代码块", async () => {
  window.history.replaceState({}, "", "/chat/mermaid-source");
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const payload = String(input).includes("/messages") ? {
      items: [{
        id: "web:mermaid-source:0",
        role: "assistant",
        content: "```mermaid\nflowchart TD\n  A[开始] --> B[完成]\n```",
        turn_id: "turn-mermaid",
        tool_chain: [],
        timestamp: "2026-07-18T10:00:00+08:00",
      }],
      total: 1,
    } : { items: [], total: 0 };
    return { ok: true, json: async () => payload } as Response;
  }));

  const { container } = render(<App />);

  await waitFor(() => expect(container.querySelector('[data-streamdown="mermaid"]')).not.toBeNull());
  expect(container.querySelector('[data-streamdown="code-block"]')).toBeNull();
  const diagramTab = screen.getByRole("button", { name: "图表" });
  const codeTab = screen.getByRole("button", { name: "代码" });
  const fullscreen = screen.getByTitle("全屏查看") as HTMLButtonElement;
  expect(diagramTab).toHaveAttribute("aria-pressed", "true");
  await waitFor(() => expect(!fullscreen.disabled || screen.queryByRole("alert") !== null).toBe(true));
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  await waitFor(() => expect(fullscreen).not.toBeDisabled());

  fireEvent.click(codeTab);
  expect(codeTab).toHaveAttribute("aria-pressed", "true");
  expect(screen.getByText(/flowchart TD/)).toBeVisible();
  expect(fullscreen).toBeDisabled();
  fireEvent.click(screen.getByTitle("复制 Mermaid 源码"));
  expect(navigator.clipboard.writeText).toHaveBeenCalledWith("flowchart TD\n  A[开始] --> B[完成]\n");

  fireEvent.click(diagramTab);
  fireEvent.click(fullscreen);
  expect(screen.getByTitle("关闭大图")).toBeVisible();
  expect(document.body.style.overflow).toBe("hidden");
  fireEvent.click(screen.getByTitle("关闭大图"));
  expect(screen.queryByTitle("关闭大图")).not.toBeInTheDocument();
});

it("流式消息中的 Mermaid fence 闭合后允许立即查看大图", async () => {
  window.history.replaceState({}, "", "/chat/stream-mermaid");
  const { container } = render(<App />);
  await screen.findByText("已连接");
  const socket = FakeWebSocket.instances[0];
  socket.onmessage?.({ data: JSON.stringify({
    type: "turn.started", request_id: "r-mermaid", session_id: "web:stream-mermaid",
    turn_id: "turn-stream-mermaid", content: "生成流程图",
  }) } as MessageEvent);
  socket.onmessage?.({ data: JSON.stringify({
    type: "answer.delta", session_id: "web:stream-mermaid", turn_id: "turn-stream-mermaid",
    delta: "```mermaid\nflowchart TD\n  A --> B\n```",
  }) } as MessageEvent);

  await waitFor(() => expect(container.querySelector('[data-streamdown="mermaid"]')).not.toBeNull());
  await waitFor(() => expect(screen.getByTitle("全屏查看")).not.toBeDisabled());
});

it("空会话不再提供 Mermaid 流程图示例", async () => {
  render(<App />);

  await screen.findByText("从一个具体问题开始对话");
  expect(screen.queryByText("画一张 Mermaid 流程图说明当前链路")).not.toBeInTheDocument();
});

it("附件选择器包含新增的代码、文档和配置格式", async () => {
  const { container } = render(<App />);
  await screen.findByText("已连接");

  const input = container.querySelector<HTMLInputElement>('input[type="file"]');
  expect(input?.accept).toContain(".java");
  expect(input?.accept).toContain(".cpp");
  expect(input?.accept).toContain(".vue");
  expect(input?.accept).toContain(".rst");
  expect(input?.accept).toContain(".graphql");
  expect(input?.accept).not.toContain(".env");
});

it("拖拽文件和粘贴图片进入统一附件队列并显示文件大小", async () => {
  const { container } = render(<App />);
  await screen.findByText("已连接");
  const composer = container.querySelector<HTMLElement>(".composer");
  const source = new File(["x".repeat(1536)], "Main.java", { type: "text/plain" });
  const image = new File(["png"], "paste.png", { type: "image/png" });

  fireEvent.drop(composer!, { dataTransfer: { files: [source] } });
  expect(screen.getByText("Main.java")).toBeVisible();
  expect(screen.getByText("1.5 KB")).toBeVisible();

  fireEvent.paste(composer!, { clipboardData: { files: [image] } });
  expect(screen.getByText("paste.png")).toBeVisible();
  expect(screen.getByText("3 B")).toBeVisible();
});

it("剪贴板只通过 DataTransferItem 暴露图片时仍可粘贴", async () => {
  const { container } = render(<App />);
  await screen.findByText("已连接");
  const composer = container.querySelector<HTMLElement>(".composer");
  const image = new File(["image"], "clipboard.png", { type: "image/png" });

  fireEvent.paste(composer!, {
    clipboardData: {
      files: [],
      items: [{ kind: "file", getAsFile: () => image }],
    },
  });

  expect(screen.getByText("clipboard.png")).toBeVisible();
});

it("文件拖到聊天页面而非精确落在输入框时仍可加入附件", async () => {
  render(<App />);
  await screen.findByText("已连接");
  const source = new File(["class Main {}"], "Main.java", { type: "text/plain" });

  fireEvent.drop(document, { dataTransfer: { types: ["Files"], files: [source] } });

  expect(screen.getByText("Main.java")).toBeVisible();
});

it("附件数量、格式和大小不符合要求时显示错误且不静默截断", async () => {
  const { container } = render(<App />);
  await screen.findByText("已连接");
  const composer = container.querySelector<HTMLElement>(".composer");
  const unsupported = new File(["PK"], "archive.zip", { type: "application/zip" });
  fireEvent.drop(composer!, { dataTransfer: { files: [unsupported] } });
  expect(screen.getByRole("alert")).toHaveTextContent("不支持的附件格式");

  const tooLarge = new File([new Uint8Array(2 * 1024 * 1024 + 1)], "large.java", { type: "text/plain" });
  fireEvent.drop(composer!, { dataTransfer: { files: [tooLarge] } });
  expect(screen.getByRole("alert")).toHaveTextContent("不能超过 2 MB");

  const files = Array.from({ length: 9 }, (_, index) => new File(["x"], `${index}.txt`, { type: "text/plain" }));
  fireEvent.drop(composer!, { dataTransfer: { files } });
  expect(screen.getByRole("alert")).toHaveTextContent("最多添加 8 个附件");
  expect(screen.queryByText("8.txt")).not.toBeInTheDocument();
});

it("ignores a stale session history response after navigating elsewhere", async () => {
  let resolveFirstHistory!: (response: Response) => void;
  const firstHistory = new Promise<Response>((resolve) => { resolveFirstHistory = resolve; });
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes("/api/chat/sessions?page=")) {
      return { ok: true, json: async () => ({ items: [{
        key: "web:first", title: "First session", first_message_content: "",
        created_at: "2026-08-01T09:00:00+08:00", updated_at: "2026-08-01T09:00:00+08:00", message_count: 1,
      }, {
        key: "web:second", title: "Second session", first_message_content: "",
        created_at: "2026-08-01T10:00:00+08:00", updated_at: "2026-08-01T10:00:00+08:00", message_count: 1,
      }], total: 2 }) } as Response;
    }
    if (url.endsWith("/api/chat/sessions/web%3Afirst/messages")) return firstHistory;
    if (url.endsWith("/api/chat/sessions/web%3Asecond/messages")) {
      return { ok: true, json: async () => ({ items: [{
        id: "web:second:0", seq: 0, role: "user", content: "second history", turn_id: "turn-second",
        timestamp: "2026-08-01T10:00:00+08:00",
      }], has_more: false }) } as Response;
    }
    return { ok: true, json: async () => ({ items: [], total: 0 }) } as Response;
  }));

  render(<App />);
  fireEvent.click(await screen.findByRole("button", { name: "First session" }));
  fireEvent.click(screen.getByRole("button", { name: "Second session" }));
  expect(await screen.findByText("second history", { selector: ".user-text" })).toBeVisible();

  await act(async () => {
    resolveFirstHistory({ ok: true, json: async () => ({ items: [{
      id: "web:first:0", seq: 0, role: "user", content: "stale first history", turn_id: "turn-first",
      timestamp: "2026-08-01T09:00:00+08:00",
    }], has_more: false }) } as Response);
    await firstHistory;
  });

  expect(window.location.pathname).toBe("/chat/second");
  expect(screen.getByText("second history", { selector: ".user-text" })).toBeVisible();
  expect(screen.queryByText("stale first history", { selector: ".user-text" })).not.toBeInTheDocument();
});
