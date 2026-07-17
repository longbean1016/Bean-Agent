import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
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
  }

  close() { this.readyState = FakeWebSocket.CLOSED; this.onclose?.(); }
}

beforeEach(() => {
  localStorage.clear();
  document.documentElement.removeAttribute("data-theme");
  systemDark = false;
  colorSchemeListener = null;
  FakeWebSocket.instances = [];
  vi.stubGlobal("WebSocket", FakeWebSocket);
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
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

it("首次连接创建 Session 并发送用户消息", async () => {
  render(<App />);

  await screen.findByText("已连接");
  await waitFor(() => expect(localStorage.getItem("beanagent.session_id")).toBe("web:component"));
  const socket = FakeWebSocket.instances[0];
  expect(socket.sent[0]).toMatchObject({ type: "session.create" });

  const input = screen.getByPlaceholderText("输入消息，或附加文本与图片");
  fireEvent.change(input, { target: { value: "组件测试消息" } });
  fireEvent.click(screen.getByRole("button", { name: "发送" }));

  await waitFor(() => expect(socket.sent.some((frame) => frame.type === "message.send" && frame.text === "组件测试消息")).toBe(true));
  expect(screen.getByText("组件测试消息")).toBeVisible();
});

it("用户向上滚动后流式增量不抢回视口并可主动回到底部", async () => {
  localStorage.setItem("beanagent.session_id", "web:scroll-lock");
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
  const conversation = container.querySelector<HTMLElement>(".conversation");
  expect(conversation).not.toBeNull();
  Object.defineProperties(conversation!, {
    scrollHeight: { configurable: true, value: 1200 },
    clientHeight: { configurable: true, value: 400 },
    scrollTop: { configurable: true, writable: true, value: 300 },
  });
  const scrollTo = vi.mocked(conversation!.scrollTo);
  await waitFor(() => expect(scrollTo).toHaveBeenCalled());
  scrollTo.mockClear();

  fireEvent.scroll(conversation!);
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
  expect(scrollTo).not.toHaveBeenCalled();
  fireEvent.click(latestButton);
  expect(scrollTo).toHaveBeenCalledWith({ top: 1200, behavior: "smooth" });
  expect(screen.queryByRole("button", { name: "回到最新消息" })).not.toBeInTheDocument();
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
  localStorage.setItem("beanagent.session_id", "web:links");
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

it("代码复制保留原始格式并将按钮图标切换为勾选", async () => {
  localStorage.setItem("beanagent.session_id", "web:code-copy");
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

it("Mermaid fenced block 作为普通代码显示且不生成图表", async () => {
  localStorage.setItem("beanagent.session_id", "web:mermaid-source");
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

  await screen.findByText(/flowchart TD/);
  const codeBlock = container.querySelector('[data-streamdown="code-block"]');
  expect(codeBlock).not.toBeNull();
  expect(codeBlock).toHaveTextContent("A[开始] --> B[完成]");
  expect(codeBlock).toHaveTextContent("mermaid");
  const diagramSvg = [...container.querySelectorAll(".message-body svg")]
    .find((svg) => !svg.closest('[data-streamdown="code-block-actions"]'));
  expect(diagramSvg).toBeUndefined();
});

it("空会话不再提供 Mermaid 流程图示例", async () => {
  render(<App />);

  await screen.findByText("从一个具体问题开始");
  expect(screen.queryByText("画一张 Mermaid 流程图说明当前链路")).not.toBeInTheDocument();
});
