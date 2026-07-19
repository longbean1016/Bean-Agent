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

it("会话列表展示第一次提问时间而不是最近更新时间", async () => {
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const payload = String(input).includes("/messages")
      ? { items: [], total: 0 }
      : {
          items: [{
            key: "web:first-time",
            first_message_content: "固定创建时间",
            created_at: "2026-01-02T10:00:00+08:00",
            updated_at: "2026-12-31T10:00:00+08:00",
          }],
          total: 1,
        };
    return { ok: true, json: async () => payload } as Response;
  }));

  const { container } = render(<App />);

  await screen.findByText("固定创建时间");
  expect(screen.getByText("1/2")).toBeVisible();
  expect(screen.queryByText("12/31")).not.toBeInTheDocument();
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
  localStorage.setItem("beanagent.session_id", "web:rename");
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (init?.method === "PATCH") {
      return { ok: true, json: async () => ({ key: "web:rename", title: "新标题" }) } as Response;
    }
    const payload = url.includes("/messages") ? { items: [], total: 0 } : {
      items: [{ key: "web:rename", first_message_content: "原始标题", title: "", created_at: new Date().toISOString(), updated_at: new Date().toISOString() }],
      total: 1,
    };
    return { ok: true, json: async () => payload } as Response;
  }));

  render(<App />);
  await screen.findByText("原始标题");
  fireEvent.click(screen.getByRole("button", { name: "打开会话“原始标题”的菜单" }));
  fireEvent.click(screen.getByRole("menuitem", { name: "重命名" }));
  const input = screen.getByRole("textbox", { name: "会话标题" });
  fireEvent.change(input, { target: { value: "新标题" } });
  fireEvent.keyDown(input, { key: "Enter" });

  expect(await screen.findByText("新标题")).toBeVisible();
  expect(fetch).toHaveBeenCalledWith(
    "/api/chat/sessions/web%3Arename",
    expect.objectContaining({ method: "PATCH" }),
  );
});

it("删除当前会话后复用新建流程并回到空白会话", async () => {
  localStorage.setItem("beanagent.session_id", "web:delete");
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
  await waitFor(() => expect(socket.sent.filter((frame) => frame.type === "session.create")).toHaveLength(1));
  await waitFor(() => expect(localStorage.getItem("beanagent.session_id")).toBe("web:component"));
  expect(screen.queryAllByText("即将删除")).toHaveLength(0);
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
  await waitFor(() => expect(conversation!.scrollTop).toBeGreaterThan(798.5));
  expect(screen.queryByRole("button", { name: "回到最新消息" })).not.toBeInTheDocument();
});

it("点击历史会话后在目标消息渲染完成时强制回到底部", async () => {
  localStorage.setItem("beanagent.session_id", "web:first");
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
  fireEvent.click(screen.getByRole("button", { name: "第二个会话" }));

  await screen.findByText("第二个会话的末尾");
  await waitFor(() => expect(conversation!.scrollTop).toBeGreaterThanOrEqual(1198));
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

it("将记忆引用渲染为编号并支持展开和复制ID", async () => {
  localStorage.setItem("beanagent.session_id", "web:citations");
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

it("Mermaid fenced block 生成受限图表而不是普通代码块", async () => {
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
  localStorage.setItem("beanagent.session_id", "web:stream-mermaid");
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

  await screen.findByText("从一个具体问题开始");
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
