import * as Collapsible from "@radix-ui/react-collapsible";
import * as Dialog from "@radix-ui/react-dialog";
import { code } from "@streamdown/code";
import { mermaid } from "@streamdown/mermaid";
import {
  AlertCircle,
  Check,
  ChevronDown,
  CircleStop,
  FileText,
  Image as ImageIcon,
  Menu,
  MessageSquarePlus,
  Paperclip,
  PlugZap,
  RefreshCw,
  SendHorizontal,
  Wrench,
  X,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from "react";
import { Streamdown } from "streamdown";

import { fetchMessages, fetchSessions, mediaUrl, uploadAttachment } from "./api";
import { initialChatState, reduceChatFrame, rowsToMessages } from "./chatReducer";
import type { ChatFrame, ChatMessage, ConnectionStatus, SessionSummary, ToolActivity } from "./types";
import { BeanWebSocketClient } from "./websocketClient";

const SESSION_STORAGE_KEY = "beanagent.session_id";
const markdownPlugins = { code, mermaid };

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
  const clientRef = useRef<BeanWebSocketClient | null>(null);
  const chatRef = useRef(chat);
  chatRef.current = chat;

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
          <ConnectionControl status={connection} onReconnect={() => clientRef.current?.reconnectNow()} />
        </header>

        {chat.error ? (
          <div className="error-banner" role="alert">
            <AlertCircle size={17} /><span>{chat.error}</span>
            <button className="icon-button" onClick={() => dispatch({ type: "ui.error.clear" })} aria-label="关闭错误"><X size={16} /></button>
          </div>
        ) : null}

        <section className="conversation" aria-live="polite">
          {chat.messages.length === 0 ? <EmptyConversation onExample={setInput} /> : chat.messages.map((message) => (
            <MessageView key={message.id} message={message} />
          ))}
        </section>

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
            <time>{formatTime(session.updated_at)}</time>
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

function MessageView({ message }: { message: ChatMessage }) {
  const isUser = message.role === "user";
  return (
    <article className={`message ${isUser ? "user-message" : "assistant-message"}`}>
      <div className="message-label">{isUser ? "你" : "BeanAgent"}</div>
      <div className="message-body">
        {message.media.length ? <AttachmentGallery paths={message.media} /> : null}
        {message.thinking ? <Thinking content={message.thinking} streaming={Boolean(message.streaming)} /> : null}
        {message.tools.length ? <div className="tool-timeline">{message.tools.map((tool) => <ToolStep key={tool.callId} tool={tool} />)}</div> : null}
        {isUser ? <p className="user-text">{message.content}</p> : message.content ? (
          <Streamdown
            key={`${message.id}-${message.streaming ? "stream" : "final"}`}
            plugins={markdownPlugins}
            controls={false}
            isAnimating={Boolean(message.streaming)}
          >
            {message.content}
          </Streamdown>
        ) : message.streaming ? <span className="stream-caret" aria-label="正在生成" /> : null}
        {message.status === "interrupted" ? <span className="interrupted-label">已停止</span> : null}
      </div>
    </article>
  );
}

function Thinking({ content, streaming }: { content: string; streaming: boolean }) {
  return (
    <Collapsible.Root className="thinking" defaultOpen={streaming}>
      <Collapsible.Trigger className="thinking-trigger">思考过程 <ChevronDown size={14} /></Collapsible.Trigger>
      <Collapsible.Content className="thinking-content"><Streamdown>{content}</Streamdown></Collapsible.Content>
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
  const previews = useMemo(() => props.files.map((file) => ({ file, url: file.type.startsWith("image/") ? URL.createObjectURL(file) : "" })), [props.files]);
  useEffect(() => () => previews.forEach((item) => item.url && URL.revokeObjectURL(item.url)), [previews]);
  return (
    <footer className="composer-wrap">
      <div className="composer">
        {previews.length ? <div className="pending-files">{previews.map(({ file, url }) => (
          <div className="pending-file" key={`${file.name}-${file.lastModified}`}>
            {url ? <img src={url} alt="" /> : <FileText size={17} />}
            <span>{file.name}</span>
            <button className="icon-button" onClick={() => props.onFiles(props.files.filter((item) => item !== file))} aria-label={`移除 ${file.name}`}><X size={14} /></button>
          </div>
        ))}</div> : null}
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
            <input type="file" multiple accept="image/png,image/jpeg,image/gif,image/webp,image/bmp,text/*,.md,.json,.toml,.yaml,.yml,.py,.js,.ts,.tsx,.csv" onChange={(event) => props.onFiles([...props.files, ...Array.from(event.target.files ?? [])].slice(0, 8))} />
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
        {["列出当前工作目录并概括项目结构", "记住我的项目偏好", "画一张 Mermaid 流程图说明当前链路"].map((text) => <button key={text} onClick={() => onExample(text)}>{text}</button>)}
      </div>
    </div>
  );
}

function errorFrame(error: unknown): ChatFrame {
  return { type: "error", request_id: "", code: "client_error", message: error instanceof Error ? error.message : "发生未知错误" };
}

function shortSession(sessionId: string): string { return sessionId ? `会话 ${sessionId.slice(-8)}` : "正在创建会话"; }
function fileName(path: string): string { return path.replaceAll("\\", "/").split("/").pop() || "附件"; }
function formatTime(value: string): string { const date = new Date(value); return Number.isNaN(date.getTime()) ? "" : new Intl.DateTimeFormat("zh-CN", { month: "numeric", day: "numeric" }).format(date); }
