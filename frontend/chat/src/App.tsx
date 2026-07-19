import * as Collapsible from "@radix-ui/react-collapsible";
import * as Dialog from "@radix-ui/react-dialog";
import { code } from "@streamdown/code";
import {
  AlertCircle,
  ArrowDown,
  Brain,
  Check,
  ChevronDown,
  CircleStop,
  Copy,
  FileText,
  Image as ImageIcon,
  Menu,
  MessageSquarePlus,
  Monitor,
  Moon,
  Paperclip,
  PlugZap,
  RefreshCw,
  SendHorizontal,
  Sun,
  Wrench,
  X,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from "react";
import type { ComponentPropsWithoutRef } from "react";
import { Streamdown } from "streamdown";
import { StickToBottom, useStickToBottomContext } from "use-stick-to-bottom";

import { fetchMessages, fetchSessions, mediaUrl, uploadAttachment } from "./api";
import { initialChatState, reduceChatFrame, rowsToMessages } from "./chatReducer";
import { parseMemoryCitations } from "./citations";
import type { MemoryCitation } from "./citations";
import { MermaidBlock } from "./MermaidBlock";
import type { ChatFrame, ChatMessage, ConnectionStatus, SessionSummary, ToolActivity } from "./types";
import { BeanWebSocketClient } from "./websocketClient";

const SESSION_STORAGE_KEY = "beanagent.session_id";
const THEME_STORAGE_KEY = "beanagent.theme";
const MAX_ATTACHMENTS = 8;
const MAX_TEXT_ATTACHMENT_SIZE = 2 * 1024 * 1024;
const MAX_IMAGE_ATTACHMENT_SIZE = 10 * 1024 * 1024;
const IMAGE_SUFFIXES = new Set([".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"]);
const TEXT_SUFFIXES = new Set([
  ".txt", ".md", ".markdown", ".py", ".json", ".toml", ".yaml", ".yml",
  ".csv", ".log", ".html", ".css", ".js", ".jsx", ".ts", ".tsx", ".xml",
  ".rst", ".adoc", ".tex", ".java", ".c", ".h", ".cpp", ".hpp", ".cs",
  ".go", ".rs", ".php", ".rb", ".swift", ".kt", ".kts", ".scala", ".lua",
  ".sh", ".bash", ".zsh", ".ps1", ".bat", ".cmd", ".sql", ".r", ".vue",
  ".svelte", ".ini", ".conf", ".cfg", ".properties", ".ndjson", ".jsonl",
  ".tsv", ".graphql", ".gql", ".dockerfile",
]);
const ATTACHMENT_ACCEPT = [
  "image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp",
  ...TEXT_SUFFIXES,
].join(",");
type ThemePreference = "light" | "system" | "dark";
const markdownPlugins = { code, renderers: [{ language: "mermaid", component: MermaidBlock }] };
const markdownComponents = { inlineCode: MemoryInlineCode };
const markdownControls = {
  code: { copy: true, download: false },
  // Streamdown 的 panZoom 开关只负责控制按钮；CSS 兼容层还会阻断画布事件，
  // 保证鼠标位于图表上时滚轮仍属于外层会话。
  table: false,
};
const markdownTranslations = {
  copied: "已复制",
  copyCode: "复制代码",
  copyLink: "复制链接",
  externalLinkWarning: "即将访问外部网站",
  openExternalLink: "打开外部链接",
  openLink: "打开链接",
};

const markdownLinkSafety = {
  enabled: false,
};

function prepareMessageMarkdown(markdown: string): string {
  let fenced = false;
  return markdown.split(/(\n)/).map((line) => {
    if (line === "\n") return line;
    const fence = line.match(/^(\s*)(```|~~~)(.*)$/);
    if (fence) {
      fenced = !fenced;
      return line;
    }
    if (fenced) return line;
    return line.replace(
      /\*\*\s*(https?:\/\/[^\s*]+)\s*\*\*([。！？；，、,.!;:]?)/gu,
      (_match, rawUrl: string, trailing: string) => {
        const url = rawUrl.replace(/[。！？；，、,.!;:]+$/gu, "");
        const punctuation = rawUrl.slice(url.length) + trailing;
        // 模型常把裸链接包进粗体标记；统一转成自动链接，避免星号泄露到界面。
        return `<${url}>${punctuation}`;
      },
    );
  }).join("");
}

function MemoryInlineCode({ children, node: _node, ...props }: ComponentPropsWithoutRef<"code"> & { node?: unknown }) {
  const value = String(children ?? "");
  const citation = value.match(/^§memory-citation:(\d+)§$/u);
  if (citation) return <span className="memory-citation-inline">[{citation[1]}]</span>;
  return <code {...props}>{children}</code>;
}

function containsClosedMermaidFence(markdown: string): boolean {
  let opening: { marker: "`" | "~"; length: number; mermaid: boolean } | null = null;
  for (const line of markdown.split("\n")) {
    if (!opening) {
      const fence = line.match(/^\s{0,3}(`{3,}|~{3,})(.*)$/);
      if (!fence) continue;
      opening = {
        marker: fence[1][0] as "`" | "~",
        length: fence[1].length,
        mermaid: /^\s*mermaid(?:\s|$)/i.test(fence[2]),
      };
      continue;
    }
    const closing = line.trim();
    if (closing.length >= opening.length && [...closing].every((char) => char === opening!.marker)) {
      if (opening.mermaid) return true;
      opening = null;
    }
  }
  return false;
}

export function App() {
  const [chat, dispatch] = useReducer(reduceChatFrame, {
    ...initialChatState,
    sessionId: localStorage.getItem(SESSION_STORAGE_KEY) ?? "",
  });
  const [connection, setConnection] = useState<ConnectionStatus>("connecting");
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [input, setInput] = useState("");
  const [files, setFiles] = useState<File[]>([]);
  const [sending, setSending] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [theme, setTheme] = useState<ThemePreference>(() => readThemePreference());
  const [systemDark, setSystemDark] = useState(() => window.matchMedia("(prefers-color-scheme: dark)").matches);
  const clientRef = useRef<BeanWebSocketClient | null>(null);
  const chatRef = useRef(chat);
  chatRef.current = chat;

  useEffect(() => {
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    const handleChange = (event: MediaQueryListEvent) => setSystemDark(event.matches);
    setSystemDark(media.matches);
    media.addEventListener("change", handleChange);
    return () => media.removeEventListener("change", handleChange);
  }, []);

  useEffect(() => {
    // preference 与实际主题分离：system 保留用户意图，系统变化只更新最终配色。
    const resolved = theme === "system" ? (systemDark ? "dark" : "light") : theme;
    document.documentElement.dataset.theme = resolved;
    localStorage.setItem(THEME_STORAGE_KEY, theme);
  }, [systemDark, theme]);

  const refreshSessions = useCallback(async () => {
    try {
      setSessions(await fetchSessions());
    } catch (error) {
      dispatch(errorFrame(error));
    }
  }, []);

  const handleFrame = useCallback((frame: ChatFrame) => {
    dispatch(frame);
    if (frame.type === "session.created") {
      localStorage.setItem(SESSION_STORAGE_KEY, frame.session_id);
    }
    if (frame.type === "message.final") void refreshSessions();
  }, [refreshSessions]);

  useEffect(() => {
    const client = new BeanWebSocketClient({
      onFrame: handleFrame,
      onStatus: (status) => {
        setConnection(status);
        if (status === "connected" && !chatRef.current.sessionId) {
          client.send({ type: "session.create", request_id: crypto.randomUUID() });
        }
      },
    });
    clientRef.current = client;
    client.connect();
    void refreshSessions();
    return () => client.close();
  }, [handleFrame, refreshSessions]);

  useEffect(() => {
    if (!chat.sessionId || chat.messages.length > 0) return;
    void loadSession(chat.sessionId, false);
    // 首次恢复只随 session_id 变化触发，避免流式消息到达时重复加载历史。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [chat.sessionId]);

  const loadSession = async (sessionId: string, closeSidebar = true) => {
    try {
      const messages = rowsToMessages(await fetchMessages(sessionId));
      localStorage.setItem(SESSION_STORAGE_KEY, sessionId);
      dispatch({ type: "ui.session.select", sessionId, messages });
      if (closeSidebar) setSidebarOpen(false);
    } catch (error) {
      dispatch(errorFrame(error));
    }
  };

  const createSession = () => {
    if (connection !== "connected") return;
    localStorage.removeItem(SESSION_STORAGE_KEY);
    dispatch({ type: "ui.session.select", sessionId: "", messages: [] });
    clientRef.current?.send({ type: "session.create", request_id: crypto.randomUUID() });
    setSidebarOpen(false);
  };

  const submit = async () => {
    const cleanText = input.trim();
    if ((!cleanText && files.length === 0) || sending || chat.activeTurnId) return;
    if (connection !== "connected" || !chat.sessionId) {
      dispatch({ type: "error", request_id: "", code: "offline", message: "连接尚未就绪，请重连后再发送" });
      return;
    }
    setSending(true);
    dispatch({ type: "ui.error.clear" });
    try {
      const uploaded = await Promise.all(files.map(uploadAttachment));
      const requestId = crypto.randomUUID();
      const optimistic: ChatMessage = {
        id: `user-${requestId}`,
        role: "user",
        content: cleanText,
        thinking: "",
        media: uploaded.map((item) => item.upload_path),
        tools: [],
      };
      dispatch({ type: "ui.user.append", message: optimistic });
      const sent = clientRef.current?.send({
        type: "message.send",
        request_id: requestId,
        session_id: chat.sessionId,
        text: cleanText,
        media: uploaded.map((item) => item.upload_path),
      });
      if (!sent) throw new Error("消息未发送，WebSocket 已断开");
      setInput("");
      setFiles([]);
    } catch (error) {
      dispatch(errorFrame(error));
    } finally {
      setSending(false);
    }
  };

  const stopTurn = () => {
    if (!chat.sessionId || !chat.activeTurnId) return;
    clientRef.current?.send({
      type: "turn.stop",
      request_id: crypto.randomUUID(),
      session_id: chat.sessionId,
    });
  };

  const sidebar = (
    <SessionSidebar
      activeSessionId={chat.sessionId}
      sessions={sessions}
      onCreate={createSession}
      onSelect={(id) => void loadSession(id)}
    />
  );

  return (
    <div className="app-shell">
      <aside className="desktop-sidebar">{sidebar}</aside>
      <main className="chat-workspace">
        <header className="topbar">
          <Dialog.Root open={sidebarOpen} onOpenChange={setSidebarOpen}>
            <Dialog.Trigger asChild>
              <button className="icon-button mobile-menu" aria-label="打开会话列表" title="会话列表">
                <Menu size={19} />
              </button>
            </Dialog.Trigger>
            <Dialog.Portal>
              <Dialog.Overlay className="dialog-overlay" />
              <Dialog.Content className="mobile-sidebar">
                <Dialog.Title className="sr-only">会话列表</Dialog.Title>
                <Dialog.Close className="icon-button sidebar-close" aria-label="关闭会话列表"><X size={18} /></Dialog.Close>
                {sidebar}
              </Dialog.Content>
            </Dialog.Portal>
          </Dialog.Root>
          <div className="brand-compact">
            <span className="brand-mark">B</span>
            <div><strong>BeanAgent</strong><span>{shortSession(chat.sessionId)}</span></div>
          </div>
          <div className="topbar-actions">
            <ConnectionControl status={connection} onReconnect={() => clientRef.current?.reconnectNow()} />
            <ThemeControl value={theme} onChange={setTheme} />
          </div>
        </header>

        {chat.error ? (
          <div className="error-banner" role="alert">
            <AlertCircle size={17} /><span>{chat.error}</span>
            <button className="icon-button" onClick={() => dispatch({ type: "ui.error.clear" })} aria-label="关闭错误"><X size={16} /></button>
          </div>
        ) : null}

        <StickToBottom className="conversation" initial="instant" resize="smooth" role="log">
          <StickToBottom.Content className="conversation-content" scrollClassName="conversation-scroll">
            {chat.messages.length === 0 ? <EmptyConversation onExample={setInput} /> : chat.messages.map((message) => (
              <MessageView key={message.id} message={message} />
            ))}
          </StickToBottom.Content>
          <ConversationAutoScroll sessionId={chat.sessionId} messages={chat.messages} active={Boolean(chat.activeTurnId)} />
          <ConversationScrollButton />
        </StickToBottom>

        <Composer
          active={Boolean(chat.activeTurnId)}
          connected={connection === "connected"}
          files={files}
          input={input}
          sending={sending}
          onFiles={setFiles}
          onInput={setInput}
          onSend={() => void submit()}
          onStop={stopTurn}
        />
      </main>
    </div>
  );
}

function ConversationAutoScroll({ sessionId, messages, active }: {
  sessionId: string;
  messages: ChatMessage[];
  active: boolean;
}) {
  const { escapedFromLock, isAtBottom, scrollToBottom } = useStickToBottomContext();
  const previousSessionRef = useRef("");
  const previousCountRef = useRef(0);

  useEffect(() => {
    // 会话切换必须在目标消息已经进入 DOM 后再定位，且不能继承上一会话的
    // escaped lock；相同消息数量的两个会话也必须触发该边界。
    if (sessionId && messages.length > 0 && previousSessionRef.current !== sessionId) {
      previousSessionRef.current = sessionId;
      previousCountRef.current = messages.length;
      void scrollToBottom({ animation: "instant", ignoreEscapes: true });
      return;
    }

    const lastMessage = messages.at(-1);
    const hasNewUserMessage = messages.length > previousCountRef.current && lastMessage?.role === "user";
    previousCountRef.current = messages.length;
    if (hasNewUserMessage) {
      void scrollToBottom({ animation: "smooth", ignoreEscapes: true });
      return;
    }
    if (active && isAtBottom && !escapedFromLock) {
      void scrollToBottom({ animation: "smooth", ignoreEscapes: false });
    }
  }, [active, escapedFromLock, isAtBottom, messages, sessionId, scrollToBottom]);

  return null;
}

function ConversationScrollButton() {
  const { isAtBottom, scrollToBottom } = useStickToBottomContext();
  if (isAtBottom) return null;
  return (
    <button
      className="scroll-latest"
      aria-label="回到最新消息"
      title="回到最新消息"
      onClick={() => void scrollToBottom({ animation: "smooth", ignoreEscapes: true })}
    >
      <ArrowDown size={18} />
    </button>
  );
}

function SessionSidebar(props: {
  sessions: SessionSummary[];
  activeSessionId: string;
  onCreate: () => void;
  onSelect: (id: string) => void;
}) {
  return (
    <div className="session-panel">
      <div className="brand-lockup"><span className="brand-mark">B</span><div><strong>BeanAgent</strong><span>Local workspace</span></div></div>
      <button className="new-chat-button" onClick={props.onCreate}><MessageSquarePlus size={17} />新建会话</button>
      <div className="session-heading">最近会话</div>
      <nav className="session-list" aria-label="会话列表">
        {props.sessions.length === 0 ? <p className="session-empty">完成第一轮对话后，会话会出现在这里。</p> : props.sessions.map((session) => (
          <button
            key={session.key}
            className={`session-row ${session.key === props.activeSessionId ? "active" : ""}`}
            onClick={() => props.onSelect(session.key)}
          >
            <span>{session.first_message_content || "未命名会话"}</span>
            <time>{formatTime(session.created_at)}</time>
          </button>
        ))}
      </nav>
    </div>
  );
}

function ConnectionControl({ status, onReconnect }: { status: ConnectionStatus; onReconnect: () => void }) {
  const label = { connecting: "连接中", connected: "已连接", reconnecting: "重连中", offline: "已断开" }[status];
  return (
    <button className={`connection-state ${status}`} aria-label={label} onClick={status === "connected" ? undefined : onReconnect} title={status === "connected" ? "WebSocket 已连接" : "点击立即重连"}>
      {status === "connected" ? <PlugZap size={15} /> : <RefreshCw size={15} className={status === "reconnecting" ? "spin" : ""} />}
      <span>{label}</span>
    </button>
  );
}

function ThemeControl({ value, onChange }: { value: ThemePreference; onChange: (value: ThemePreference) => void }) {
  const options = [
    { value: "light" as const, label: "浅色", icon: Sun },
    { value: "system" as const, label: "跟随系统", icon: Monitor },
    { value: "dark" as const, label: "深色", icon: Moon },
  ];
  return (
    <div className="theme-control" role="group" aria-label="主题模式">
      {options.map((option) => {
        const Icon = option.icon;
        return (
          <button
            key={option.value}
            className={value === option.value ? "active" : ""}
            aria-label={option.label}
            aria-pressed={value === option.value}
            title={option.label}
            onClick={() => onChange(option.value)}
          >
            <Icon size={14} /><span>{option.label}</span>
          </button>
        );
      })}
    </div>
  );
}

function MessageView({ message }: { message: ChatMessage }) {
  const isUser = message.role === "user";
  const parsed = useMemo(() => parseMemoryCitations(message.content), [message.content]);
  // Streamdown 在 isAnimating 期间会禁用复制和全屏按钮。Mermaid fence 一旦
  // 闭合就已经具备稳定源码，应立即放开查看大图，而不必等待 final 帧。
  const markdownAnimating = Boolean(message.streaming && !containsClosedMermaidFence(message.content));

  return (
    <article className={`message ${isUser ? "user-message" : "assistant-message"}`}>
      <div className="message-label">{isUser ? "你" : "BeanAgent"}</div>
      <div className="message-body">
        {message.media.length ? <AttachmentGallery paths={message.media} /> : null}
        {message.thinking ? <Thinking content={message.thinking} streaming={Boolean(message.streaming)} /> : null}
        {message.tools.length ? <div className="tool-timeline">{message.tools.map((tool) => <ToolStep key={tool.callId} tool={tool} />)}</div> : null}
        {isUser ? <p className="user-text">{message.content}</p> : message.content ? (
          <div className="beanagent-markdown">
            <Streamdown
              key={`${message.id}-${message.streaming ? "stream" : "final"}`}
              plugins={markdownPlugins}
              components={markdownComponents}
              controls={markdownControls}
              isAnimating={markdownAnimating}
              lineNumbers={false}
              linkSafety={markdownLinkSafety}
              translations={markdownTranslations}
            >
              {prepareMessageMarkdown(parsed.markdown)}
            </Streamdown>
          </div>
        ) : message.streaming ? <span className="stream-caret" aria-label="正在生成" /> : null}
        {!isUser && parsed.citations.length ? <MemoryCitationList citations={parsed.citations} /> : null}
        {message.status === "interrupted" ? <span className="interrupted-label">已停止</span> : null}
      </div>
    </article>
  );
}

function MemoryCitationList({ citations }: { citations: MemoryCitation[] }) {
  const [expanded, setExpanded] = useState<number | null>(null);
  const [copied, setCopied] = useState("");
  const active = citations.find((citation) => citation.number === expanded);

  const copyId = async (id: string) => {
    await navigator.clipboard.writeText(id);
    setCopied(id);
  };

  return (
    <aside className="memory-citations" aria-label="记忆引用">
      <span className="memory-citations-label">记忆引用</span>
      <div className="memory-citation-chips">
        {citations.map((citation) => (
          <button
            id={`memory-citation-${citation.number}`}
            key={citation.number}
            type="button"
            className="memory-citation-chip"
            aria-label={`查看引用 ${citation.number}`}
            aria-expanded={expanded === citation.number}
            onClick={() => setExpanded(expanded === citation.number ? null : citation.number)}
          >
            [{citation.number}]
          </button>
        ))}
      </div>
      {active ? (
        <div className="memory-citation-detail">
          <strong>引用 {active.number}</strong>
          {active.ids.map((id) => (
            <div className="memory-citation-id" key={id}>
              <span className="memory-citation-prefix">记忆 ID：</span>
              <code>{id}</code>
              <button
                type="button"
                className="memory-citation-copy"
                aria-label={`复制记忆ID ${id}`}
                title={copied === id ? "已复制" : "复制记忆 ID"}
                onClick={() => void copyId(id)}
              >
                {copied === id ? <Check size={14} /> : <Copy size={14} />}
              </button>
            </div>
          ))}
        </div>
      ) : null}
    </aside>
  );
}

function Thinking({ content, streaming }: { content: string; streaming: boolean }) {
  return (
    <Collapsible.Root className="thinking" defaultOpen={streaming}>
      <Collapsible.Trigger className="thinking-trigger">
        <Brain size={14} />
        <span>{streaming ? "正在思考…" : "思考完成"}</span>
        <ChevronDown size={14} />
      </Collapsible.Trigger>
      <Collapsible.Content className="thinking-content beanagent-markdown">
        <Streamdown plugins={markdownPlugins} controls={markdownControls} lineNumbers={false} linkSafety={markdownLinkSafety} translations={markdownTranslations}>
          {prepareMessageMarkdown(content)}
        </Streamdown>
      </Collapsible.Content>
    </Collapsible.Root>
  );
}

function ToolStep({ tool }: { tool: ToolActivity }) {
  const icon = tool.status === "completed" ? <Check size={14} /> : tool.status === "error" ? <AlertCircle size={14} /> : <Wrench size={14} />;
  return (
    <Collapsible.Root className={`tool-step ${tool.status}`}>
      <Collapsible.Trigger className="tool-trigger">
        <span className="tool-icon">{icon}</span><strong>{tool.name}</strong><span>{tool.status === "running" ? "执行中" : tool.status === "error" ? "失败" : "完成"}</span><ChevronDown size={14} />
      </Collapsible.Trigger>
      <Collapsible.Content className="tool-detail">
        <pre>{JSON.stringify(tool.arguments, null, 2)}</pre>
        {tool.resultPreview ? <p>{tool.resultPreview}</p> : null}
      </Collapsible.Content>
    </Collapsible.Root>
  );
}

function AttachmentGallery({ paths }: { paths: string[] }) {
  return <div className="attachment-gallery">{paths.map((path) => {
    const image = /\.(png|jpe?g|gif|webp|bmp)$/i.test(path);
    return image ? <a href={mediaUrl(path)} target="_blank" rel="noreferrer" key={path}><img src={mediaUrl(path)} alt={fileName(path)} /></a> : (
      <a className="file-attachment" href={mediaUrl(path)} target="_blank" rel="noreferrer" key={path}><FileText size={17} /><span>{fileName(path)}</span></a>
    );
  })}</div>;
}

function Composer(props: {
  input: string; files: File[]; active: boolean; connected: boolean; sending: boolean;
  onInput: (value: string) => void; onFiles: (files: File[]) => void; onSend: () => void; onStop: () => void;
}) {
  const [attachmentError, setAttachmentError] = useState("");
  const [dragging, setDragging] = useState(false);
  const previews = useMemo(() => props.files.map((file) => ({ file, url: file.type.startsWith("image/") ? URL.createObjectURL(file) : "" })), [props.files]);
  useEffect(() => () => previews.forEach((item) => item.url && URL.revokeObjectURL(item.url)), [previews]);

  const addFiles = useCallback((incoming: File[]) => {
    // 选择、拖拽和粘贴共用同一入口；这里只负责即时反馈，服务端仍执行最终内容校验。
    setAttachmentError("");
    if (props.files.length + incoming.length > MAX_ATTACHMENTS) {
      setAttachmentError(`最多添加 ${MAX_ATTACHMENTS} 个附件`);
      return;
    }
    for (const file of incoming) {
      const suffix = fileSuffix(file.name);
      const image = IMAGE_SUFFIXES.has(suffix) && file.type.startsWith("image/");
      const text = TEXT_SUFFIXES.has(suffix);
      if (!image && !text) {
        setAttachmentError(`不支持的附件格式：${file.name}`);
        return;
      }
      const limit = image ? MAX_IMAGE_ATTACHMENT_SIZE : MAX_TEXT_ATTACHMENT_SIZE;
      if (file.size > limit) {
        setAttachmentError(`${file.name} 不能超过 ${image ? 10 : 2} MB`);
        return;
      }
    }
    props.onFiles([...props.files, ...incoming]);
  }, [props.files, props.onFiles]);

  useEffect(() => {
    // 与 Akashic 一致使用原生页面级监听，文件落在会话区或输入框外围时也不会被浏览器打开。
    const hasFiles = (event: DragEvent) => {
      const transfer = event.dataTransfer;
      return !!transfer && (transfer.types?.includes("Files") || transfer.files.length > 0);
    };
    const handleDragOver = (event: DragEvent) => {
      if (!hasFiles(event)) return;
      event.preventDefault();
      setDragging(true);
    };
    const handleDrop = (event: DragEvent) => {
      if (!hasFiles(event)) return;
      event.preventDefault();
      setDragging(false);
      const dropped = Array.from(event.dataTransfer?.files ?? []);
      if (dropped.length) addFiles(dropped);
    };
    const handleDragEnd = () => setDragging(false);
    document.addEventListener("dragover", handleDragOver);
    document.addEventListener("drop", handleDrop);
    document.addEventListener("dragleave", handleDragEnd);
    return () => {
      document.removeEventListener("dragover", handleDragOver);
      document.removeEventListener("drop", handleDrop);
      document.removeEventListener("dragleave", handleDragEnd);
    };
  }, [addFiles]);

  return (
    <footer className="composer-wrap">
      <div
        className={`composer${dragging ? " dragging" : ""}`}
        onPaste={(event) => {
          // 浏览器复制截图时经常只填充 items 而不填充 files，必须兼容两种数据来源。
          const direct = Array.from(event.clipboardData.files);
          const pasted = direct.length ? direct : Array.from(event.clipboardData.items)
            .filter((item) => item.kind === "file")
            .map((item) => item.getAsFile())
            .filter((file): file is File => file !== null);
          if (!pasted.length) return;
          event.preventDefault();
          addFiles(pasted);
        }}
      >
        {previews.length ? <div className="pending-files">{previews.map(({ file, url }) => (
          <div className="pending-file" key={`${file.name}-${file.lastModified}`}>
            {url ? <img src={url} alt="" /> : <FileText size={17} />}
            <span className="pending-file-info"><span>{file.name}</span><small>{formatFileSize(file.size)}</small></span>
            <button className="icon-button" onClick={() => props.onFiles(props.files.filter((item) => item !== file))} aria-label={`移除 ${file.name}`}><X size={14} /></button>
          </div>
        ))}</div> : null}
        {attachmentError ? <div className="attachment-error" role="alert">{attachmentError}</div> : null}
        <textarea
          value={props.input}
          onChange={(event) => props.onInput(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); props.onSend(); }
          }}
          placeholder={props.connected ? "输入消息，或附加文本与图片" : "等待连接恢复"}
          disabled={!props.connected}
          rows={3}
        />
        <div className="composer-actions">
          <label className="icon-button attach-button" title="添加文本或图片">
            <Paperclip size={18} /><span className="sr-only">添加附件</span>
            <input type="file" multiple accept={ATTACHMENT_ACCEPT} onChange={(event) => { addFiles(Array.from(event.target.files ?? [])); event.target.value = ""; }} />
          </label>
          <span className="composer-hint">Enter 发送 · Shift+Enter 换行</span>
          {props.active ? (
            <button className="send-button stop" aria-label="停止" onClick={props.onStop}><CircleStop size={18} /><span>停止</span></button>
          ) : (
            <button className="send-button" aria-label="发送" onClick={props.onSend} disabled={props.sending || (!props.input.trim() && props.files.length === 0)}><SendHorizontal size={18} /><span>发送</span></button>
          )}
        </div>
      </div>
    </footer>
  );
}

function EmptyConversation({ onExample }: { onExample: (text: string) => void }) {
  return (
    <div className="empty-conversation">
      <span className="empty-mark">B</span><h1>从一个具体问题开始</h1><p>BeanAgent 可以读取工作区、调用工具并记住重要信息。</p>
      <div className="example-prompts">
        {["列出当前工作目录并概括项目结构", "记住我的项目偏好", "解释一段代码并指出潜在风险"].map((text) => <button key={text} onClick={() => onExample(text)}>{text}</button>)}
      </div>
    </div>
  );
}

function errorFrame(error: unknown): ChatFrame {
  return { type: "error", request_id: "", code: "client_error", message: error instanceof Error ? error.message : "发生未知错误" };
}

function readThemePreference(): ThemePreference {
  const stored = localStorage.getItem(THEME_STORAGE_KEY);
  return stored === "light" || stored === "dark" || stored === "system" ? stored : "system";
}

function shortSession(sessionId: string): string { return sessionId ? `会话 ${sessionId.slice(-8)}` : "正在创建会话"; }
function fileName(path: string): string { return path.replaceAll("\\", "/").split("/").pop() || "附件"; }
function fileSuffix(name: string): string { const index = name.lastIndexOf("."); return index >= 0 ? name.slice(index).toLowerCase() : ""; }
function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Number((bytes / 1024).toFixed(1))} KB`;
  return `${Number((bytes / 1024 / 1024).toFixed(1))} MB`;
}
function formatTime(value: string): string { const date = new Date(value); return Number.isNaN(date.getTime()) ? "" : new Intl.DateTimeFormat("zh-CN", { month: "numeric", day: "numeric" }).format(date); }
