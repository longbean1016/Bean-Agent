import * as Collapsible from "@radix-ui/react-collapsible";
import * as Dialog from "@radix-ui/react-dialog";
import { code } from "@streamdown/code";
import {
  AlertCircle,
  ArrowDown,
  Brain,
  Bell,
  Check,
  ChevronDown,
  CircleStop,
  Copy,
  FileText,
  Image as ImageIcon,
  Menu,
  MessageSquarePlus,
  Mic,
  MoreHorizontal,
  Monitor,
  Moon,
  Paperclip,
  Pencil,
  PlugZap,
  RefreshCw,
  SendHorizontal,
  Sun,
  Trash2,
  Wrench,
  X,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type { ComponentPropsWithoutRef, ReactNode } from "react";
import { Streamdown } from "streamdown";
import { StickToBottom, useStickToBottomContext } from "use-stick-to-bottom";

import { deleteReminder, deleteSession, fetchMessagePage, fetchMessagesAroundPage, fetchNotifications, fetchOlderMessages, fetchProactiveSettings, fetchReminders, fetchSessions, fetchTurns, mediaUrl, renameSession, saveProactiveSettings, uploadAttachment } from "./api";
import { idleTurnState, initialChatState, mergeTimeline, notificationRowsToMessages, reduceChatFrame, rowsToMessages } from "./chatReducer";
import { parseMemoryCitations } from "./citations";
import type { MemoryCitation } from "./citations";
import { MermaidBlock } from "./MermaidBlock";
import { pathForSession, routeKey, sessionFromPath } from "./chatRoute";
import { groupSessionsByUpdatedAt } from "./sessionGroups";
import type { ChatFrame, ChatMessage, ConnectionStatus, MessageRow, ProactiveSettings, ScheduledReminder, SessionSummary, ToolActivity, TurnNavigationEntry } from "./types";
import { groupMessagesIntoNavigationTurns, TurnNavigator, turnsFromMessages } from "./TurnNavigator";
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
type MessageWindowState = { hasMoreBefore: boolean; nextBeforeSeq: number | null };
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

type BrowserSpeechRecognition = {
  continuous: boolean;
  interimResults: boolean;
  lang: string;
  onstart: (() => void) | null;
  onend: (() => void) | null;
  onerror: ((event: { error?: string }) => void) | null;
  onresult: ((event: { resultIndex: number; results: ArrayLike<{ isFinal: boolean; 0: { transcript: string } }> }) => void) | null;
  abort: () => void;
  start: () => void;
  stop: () => void;
};

type BrowserSpeechRecognitionConstructor = new () => BrowserSpeechRecognition;

function getSpeechRecognitionConstructor(): BrowserSpeechRecognitionConstructor | null {
  const speechWindow = window as Window & {
    SpeechRecognition?: BrowserSpeechRecognitionConstructor;
    webkitSpeechRecognition?: BrowserSpeechRecognitionConstructor;
  };
  return speechWindow.SpeechRecognition ?? speechWindow.webkitSpeechRecognition ?? null;
}

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
  const initialRouteSession = sessionFromPath(window.location.pathname);
  const initialSession = initialRouteSession || readStoredSession();
  const [chat, dispatch] = useReducer(reduceChatFrame, {
    ...initialChatState,
    sessionId: initialSession,
  });
  const [routeSession, setRouteSession] = useState(initialSession);
  const [connection, setConnection] = useState<ConnectionStatus>("connecting");
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [turnsBySession, setTurnsBySession] = useState<Record<string, TurnNavigationEntry[]>>({});
  const [notificationsBySession, setNotificationsBySession] = useState<Record<string, ChatMessage[]>>({});
  const [messageWindows, setMessageWindows] = useState<Record<string, MessageWindowState>>({});
  const [loadingOlderSessionId, setLoadingOlderSessionId] = useState("");
  const [loadingSessionId, setLoadingSessionId] = useState(initialSession);
  const [input, setInput] = useState("");
  const [files, setFiles] = useState<File[]>([]);
  const [textDrafts, setTextDrafts] = useState<Record<string, string>>({});
  const [fileDrafts, setFileDrafts] = useState<Record<string, File[]>>({});
  const [sending, setSending] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [theme, setTheme] = useState<ThemePreference>(() => readThemePreference());
  const [systemDark, setSystemDark] = useState(() => window.matchMedia("(prefers-color-scheme: dark)").matches);
  const [editingTitle, setEditingTitle] = useState(false);
  const [titleDraft, setTitleDraft] = useState("");
  const clientRef = useRef<BeanWebSocketClient | null>(null);
  const chatRef = useRef(chat);
  const messageWindowsRef = useRef(messageWindows);
  const notificationsBySessionRef = useRef(notificationsBySession);
  const loadingOlderSessionIdRef = useRef(loadingOlderSessionId);
  const routeSessionRef = useRef(routeSession);
  chatRef.current = chat;
  messageWindowsRef.current = messageWindows;
  notificationsBySessionRef.current = notificationsBySession;
  loadingOlderSessionIdRef.current = loadingOlderSessionId;
  routeSessionRef.current = routeSession;
  const currentTurn = chat.turnStates[chat.sessionId] ?? idleTurnState;
  const turnActive = currentTurn.status === "submitting" || currentTurn.status === "queued" || currentTurn.status === "running";
  const restoringSession = Boolean(chat.sessionId && loadingSessionId === chat.sessionId && chat.messages.length === 0);
  const messageTurns = useMemo(() => turnsFromMessages(chat.messages), [chat.messages]);
  const conversationTurns = useMemo(() => (
    mergeNavigationTurns(turnsBySession[chat.sessionId] ?? [], messageTurns)
  ), [chat.sessionId, messageTurns, turnsBySession]);
  const conversationTurnGroups = useMemo(() => groupMessagesIntoNavigationTurns(chat.messages), [chat.messages]);

  useEffect(() => {
    if (!initialRouteSession && initialSession) {
      window.history.replaceState({}, "", pathForSession(initialSession));
    }
    // 初始路径归一化只在首屏执行，后续切换会话由 selectSession/createSession 接管。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

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
      const loaded = await fetchSessions();
      setSessions((current) => {
        const loadedKeys = new Set(loaded.map((session) => session.key));
        // 初始列表请求可能晚于发送请求返回；保留尚未被服务端目录确认的临时会话。
        const pending = current.filter((session) => (
          session.title === "新对话"
          && session.message_count === 0
          && !loadedKeys.has(session.key)
        ));
        return [...pending, ...loaded];
      });
    } catch (error) {
      dispatch(errorFrame(error));
    }
  }, []);

  const refreshTurnPreviews = useCallback(async (sessionId: string) => {
    if (!sessionId) return;
    try {
      const turns = await fetchTurns(sessionId);
      setTurnsBySession((current) => ({ ...current, [sessionId]: turns }));
    } catch (error) {
      dispatch(errorFrame(error));
    }
  }, []);

  const handleNotificationFrame = useCallback((frame: ChatFrame) => {
    if (!isNotificationFinalFrame(frame)) return false;
    const notification = notificationFrameToMessage(frame);
    if (!notification) return true;
    const sessionId = frame.session_id;
    const currentNotifications = notificationsBySessionRef.current[sessionId] ?? [];
    const nextNotifications = upsertMessage(currentNotifications, notification);
    setNotificationsBySession((current) => ({
      ...current,
      [sessionId]: upsertMessage(current[sessionId] ?? [], notification),
    }));
    if (sessionId === chatRef.current.sessionId) {
      const persistedMessages = chatRef.current.messages.filter((message) => !isStandaloneNotification(message));
      dispatch({
        type: "ui.session.select",
        sessionId,
        messages: mergeMessageWindow(persistedMessages, nextNotifications),
      });
    }
    return true;
  }, []);

  const handleFrame = useCallback((frame: ChatFrame) => {
    if (handleNotificationFrame(frame)) return;
    dispatch(frame);
    if (frame.type === "session.created") {
      localStorage.setItem(SESSION_STORAGE_KEY, frame.session_id);
    }
    if (frame.type === "session.updated") {
      setSessions((current) => upsertSessionSummary(current, frame.session));
    }
    if (frame.type === "turn.started") {
      // 收到后端接收确认后立即提升已有会话；最终提交后再通过列表接口校准持久化时间。
      const acknowledgedAt = new Date().toISOString();
      setSessions((current) => current.map((session) => (
        session.key === frame.session_id
          ? { ...session, updated_at: acknowledgedAt }
          : session
      )));
    }
    if (frame.type === "message.final") {
      void refreshSessions();
      void refreshTurnPreviews(frame.session_id);
    }
  }, [handleNotificationFrame, refreshSessions, refreshTurnPreviews]);

  useEffect(() => {
    const client = new BeanWebSocketClient({
      onFrame: handleFrame,
      onStatus: (status) => {
        setConnection(status);
        if (status === "connected" && !routeSessionRef.current && !chatRef.current.sessionId) {
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
    if (connection !== "connected" || !chat.sessionId) return;
    clientRef.current?.send({ type: "session.subscribe", request_id: crypto.randomUUID(), session_id: chat.sessionId });
  }, [chat.sessionId, connection]);

  useEffect(() => {
    if (!chat.sessionId || chat.messages.length > 0) return;
    void loadSession(chat.sessionId, false);
    // 首次恢复只随 session_id 变化触发，避免流式消息到达时重复加载历史。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [chat.sessionId]);

  const loadSession = async (sessionId: string, closeSidebar = true) => {
    setLoadingSessionId(sessionId);
    try {
      const [page, notifications, turns] = await Promise.all([
        fetchMessagePage(sessionId),
        fetchNotifications(sessionId),
        fetchTurns(sessionId),
      ]);
      const rows = page.items ?? [];
      const notificationMessages = notificationRowsToMessages(notifications);
      const messages = mergeMessageWindow(rowsToMessages(rows), notificationMessages);
      setMessageWindows((current) => ({
        ...current,
        [sessionId]: {
          hasMoreBefore: Boolean(page.has_more),
          nextBeforeSeq: page.next_before_seq ?? firstSeqFromRows(rows),
        },
      }));
      setNotificationsBySession((current) => ({ ...current, [sessionId]: notificationMessages }));
      setTurnsBySession((current) => ({ ...current, [sessionId]: turns }));
      localStorage.setItem(SESSION_STORAGE_KEY, sessionId);
      dispatch({ type: "ui.session.select", sessionId, messages });
      if (closeSidebar) setSidebarOpen(false);
    } catch (error) {
      dispatch(errorFrame(error));
    } finally {
      setLoadingSessionId((current) => current === sessionId ? "" : current);
    }
  };

  const createSession = () => {
    if (connection !== "connected") return;
    setLoadingSessionId("");
    localStorage.removeItem(SESSION_STORAGE_KEY);
    window.history.pushState({}, "", "/");
    setRouteSession("");
    dispatch({ type: "ui.session.select", sessionId: "", messages: [] });
    clientRef.current?.send({ type: "session.create", request_id: crypto.randomUUID() });
    // 每次点击新建都创建全新的根页面草稿，不影响任何已有会话的独立草稿。
    setTextDrafts((current) => ({ ...current, [routeKey("")]: "" }));
    setFileDrafts((current) => ({ ...current, [routeKey("")]: [] }));
    setInput("");
    setFiles([]);
    setSidebarOpen(false);
  };

  const selectSession = (sessionId: string) => {
    window.history.pushState({}, "", pathForSession(sessionId));
    setRouteSession(sessionId);
    setInput(textDrafts[routeKey(sessionId)] ?? "");
    setFiles(fileDrafts[routeKey(sessionId)] ?? []);
    void loadSession(sessionId);
  };

  const handleTurnRequest = useCallback(async (turn: TurnNavigationEntry) => {
    const sessionId = chatRef.current.sessionId;
    if (!sessionId || typeof turn.seq !== "number") return;
    const loaded = chatRef.current.messages.some((message) => (
      message.role === "user"
      && (message.turnId === turn.id || message.id === turn.id || message.seq === turn.seq)
    ));
    if (loaded) return;
    try {
      const [page, notifications] = await Promise.all([
        fetchMessagesAroundPage(sessionId, turn.seq),
        fetchNotifications(sessionId),
      ]);
      const rows = page.items ?? [];
      const notificationMessages = notificationRowsToMessages(notifications);
      const messages = mergeMessageWindow(rowsToMessages(rows), notificationMessages);
      setMessageWindows((current) => ({
        ...current,
        [sessionId]: {
          hasMoreBefore: Boolean(page.has_before),
          nextBeforeSeq: page.next_before_seq ?? firstSeqFromRows(rows),
        },
      }));
      setNotificationsBySession((current) => ({ ...current, [sessionId]: notificationMessages }));
      dispatch({ type: "ui.session.select", sessionId, messages });
    } catch (error) {
      dispatch(errorFrame(error));
    }
  }, []);

  const loadOlderMessages = useCallback(async (scroller: HTMLElement) => {
    const sessionId = chatRef.current.sessionId;
    const windowState = messageWindowsRef.current[sessionId];
    if (!sessionId || !windowState?.hasMoreBefore || typeof windowState.nextBeforeSeq !== "number") return;
    if (loadingOlderSessionIdRef.current === sessionId) return;
    const previousHeight = scroller.scrollHeight;
    const previousTop = scroller.scrollTop;
    setLoadingOlderSessionId(sessionId);
    try {
      const page = await fetchOlderMessages(sessionId, windowState.nextBeforeSeq);
      const olderMessages = rowsToMessages(page.items ?? []);
      const currentMessages = chatRef.current.messages.filter((message) => !isStandaloneNotification(message));
      const persistedMessages = dedupeMessages(mergeTimeline(olderMessages, currentMessages));
      const messages = mergeMessageWindow(persistedMessages, notificationsBySessionRef.current[sessionId] ?? []);
      dispatch({ type: "ui.session.select", sessionId, messages });
      setMessageWindows((current) => ({
        ...current,
        [sessionId]: {
          hasMoreBefore: Boolean(page.has_more),
          nextBeforeSeq: page.next_before_seq ?? firstSeqFromRows(page.items ?? []),
        },
      }));
      requestAnimationFrame(() => {
        const delta = scroller.scrollHeight - previousHeight;
        scroller.scrollTop = Math.max(0, previousTop + delta);
      });
    } catch (error) {
      dispatch(errorFrame(error));
    } finally {
      setLoadingOlderSessionId((current) => current === sessionId ? "" : current);
    }
  }, []);

  useEffect(() => {
    if (!chat.sessionId) return;
    const scroller = document.querySelector<HTMLElement>(".conversation-scroll");
    if (!scroller) return;
    const handleScroll = () => {
      if (scroller.scrollTop > 48) return;
      void loadOlderMessages(scroller);
    };
    scroller.addEventListener("scroll", handleScroll, { passive: true });
    return () => scroller.removeEventListener("scroll", handleScroll);
  }, [chat.sessionId, loadOlderMessages]);

  useEffect(() => {
    const handlePopState = () => {
      const sessionId = sessionFromPath(window.location.pathname);
      setRouteSession(sessionId);
      setInput(textDrafts[routeKey(sessionId)] ?? "");
      setFiles(fileDrafts[routeKey(sessionId)] ?? []);
      if (sessionId) {
        void loadSession(sessionId);
        return;
      }
      dispatch({ type: "ui.session.select", sessionId: "", messages: [] });
      if (connection === "connected") {
        clientRef.current?.send({ type: "session.create", request_id: crypto.randomUUID() });
      }
    };
    window.addEventListener("popstate", handlePopState);
    return () => window.removeEventListener("popstate", handlePopState);
  }, [connection, fileDrafts, textDrafts]);

  const handleRenameSession = async (sessionId: string, title: string) => {
    try {
      await renameSession(sessionId, title);
      // PATCH 成功后重新读取服务端目录，确保分组和顺序使用数据库中的最终 updated_at。
      await refreshSessions();
    } catch (error) {
      dispatch(errorFrame(error));
      throw error;
    }
  };

  const submit = async () => {
    const cleanText = input.trim();
    if ((!cleanText && files.length === 0) || sending || turnActive) return;
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
      dispatch({ type: "ui.turn.submitted", sessionId: chat.sessionId, requestId });
      const sent = clientRef.current?.send({
        type: "message.send",
        request_id: requestId,
        session_id: chat.sessionId,
        text: cleanText,
        media: uploaded.map((item) => item.upload_path),
      });
      if (!sent) {
        dispatch({
          type: "error",
          request_id: requestId,
          session_id: chat.sessionId,
          code: "closed",
          message: "消息未发送，WebSocket 已断开",
        });
        return;
      }
      if (!routeSession) {
        window.history.pushState({}, "", pathForSession(chat.sessionId));
        setRouteSession(chat.sessionId);
      }
      const submittedAt = new Date().toISOString();
      // 新会话的首轮可能长期运行或排队，发送成功后先放入目录，最终再由服务端标题覆盖。
      setSessions((current) => current.some((session) => session.key === chat.sessionId)
        ? current
        : [{
            key: chat.sessionId,
            title: "新对话",
            created_at: submittedAt,
            updated_at: submittedAt,
            message_count: 0,
            first_message_content: "",
          }, ...current]);
      setInput("");
      setFiles([]);
      setTextDrafts((current) => ({ ...current, [routeKey(routeSession || chat.sessionId)]: "", [routeKey("")]: "" }));
      setFileDrafts((current) => ({ ...current, [routeKey(routeSession || chat.sessionId)]: [], [routeKey("")]: [] }));
    } catch (error) {
      dispatch(errorFrame(error));
    } finally {
      setSending(false);
    }
  };

  const stopTurn = () => {
    if (!chat.sessionId || !turnActive) return;
    clientRef.current?.send({
      type: "turn.stop",
      request_id: crypto.randomUUID(),
      session_id: chat.sessionId,
    });
  };

  const handleDeleteSession = async (sessionId: string) => {
    try {
      if (sessionId === chat.sessionId) stopTurn();
      await deleteSession(sessionId);
      setSessions((current) => current.filter((session) => session.key !== sessionId));
      if (sessionId === chat.sessionId) createSession();
    } catch (error) {
      dispatch(errorFrame(error));
      throw error;
    }
  };

  const sidebar = (
    <SessionSidebar
      activeSessionId={chat.sessionId}
      sessions={sessions}
      onCreate={createSession}
      onDelete={handleDeleteSession}
      onRename={handleRenameSession}
      onSelect={selectSession}
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
          {(() => {
            const current = sessions.find((s) => s.key === chat.sessionId);
            const title = current?.title || current?.first_message_content || "";
            return chat.sessionId && title && title !== "新对话" ? (
              <div className="topbar-session-title">
                {editingTitle ? (
                  <input
                    className="topbar-title-input"
                    autoFocus
                    maxLength={60}
                    value={titleDraft}
                    onBlur={() => {
                      const t = titleDraft.trim();
                      const orig = current?.title || current?.first_message_content || "新对话";
                      if (t && t !== orig) void handleRenameSession(chat.sessionId, t);
                      setEditingTitle(false);
                    }}
                    onChange={(e) => setTitleDraft(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") {
                        const t = titleDraft.trim();
                        const orig = current?.title || current?.first_message_content || "新对话";
                        if (t && t !== orig) void handleRenameSession(chat.sessionId, t);
                        setEditingTitle(false);
                      }
                      if (e.key === "Escape") setEditingTitle(false);
                    }}
                  />
                ) : (
                  <button
                    className="topbar-title-display"
                    onClick={() => {
                      setTitleDraft(title);
                      setEditingTitle(true);
                    }}
                    title="点击重命名"
                  >
                    {title.length > 30 ? title.slice(0, 30) + "…" : title}
                  </button>
                )}
              </div>
            ) : null;
          })()}
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
            {restoringSession ? <ConversationSkeleton /> : chat.messages.length === 0 ? <EmptyConversation /> : conversationTurnGroups.map((group) => (
              <section
                key={group.messages[0].id}
                className="turn-section"
                data-turn-region={group.navigationTurnId || undefined}
              >
                {group.messages.map((message) => (
                  <MessageView key={message.id} message={message} navigationTurnId={group.navigationTurnId} />
                ))}
              </section>
            ))}
          </StickToBottom.Content>
          <TurnNavigator sessionId={chat.sessionId} turns={conversationTurns} onTurnRequest={handleTurnRequest} />
          <ConversationAutoScroll sessionId={chat.sessionId} messages={chat.messages} active={currentTurn.status === "running"} />
          <ConversationScrollButton />
        </StickToBottom>

        <Composer
          active={turnActive}
          turnStatus={currentTurn.status}
          queuePosition={currentTurn.queuePosition}
          connected={connection === "connected"}
          files={files}
          input={input}
          sessionId={chat.sessionId}
          sending={sending}
          onFiles={(next) => {
            setFiles(next);
            setFileDrafts((current) => ({ ...current, [routeKey(routeSession)]: next }));
          }}
          onInput={(value) => {
            setInput(value);
            setTextDrafts((current) => ({ ...current, [routeKey(routeSession)]: value }));
          }}
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
  onDelete: (id: string) => Promise<void>;
  onRename: (id: string, title: string) => Promise<void>;
  onSelect: (id: string) => void;
}) {
  const groups = useMemo(() => groupSessionsByUpdatedAt(props.sessions), [props.sessions]);
  const [menuSessionId, setMenuSessionId] = useState("");
  const [editingSessionId, setEditingSessionId] = useState("");
  const [titleDraft, setTitleDraft] = useState("");
  const [deleteTarget, setDeleteTarget] = useState<SessionSummary | null>(null);
  const [proactiveTarget, setProactiveTarget] = useState<SessionSummary | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [scrollbarVisible, setScrollbarVisible] = useState(false);
  const scrollbarHideTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const sessionListRef = useRef<HTMLElement>(null);

  useEffect(() => {
    // 新会话发送消息后出现在侧栏时，自动滚动定位到该会话
    if (sessionListRef.current) {
      const activeRow = sessionListRef.current.querySelector(".session-row.active");
      if (activeRow && typeof activeRow.scrollIntoView === "function") {
        try {
          activeRow.scrollIntoView({ behavior: "smooth", block: "nearest" });
        } catch {
          // jsdom 环境可能不支持 scrollIntoView，忽略
        }
      }
    }
  }, [props.activeSessionId, props.sessions]);

  useEffect(() => () => {
    if (scrollbarHideTimerRef.current !== null) clearTimeout(scrollbarHideTimerRef.current);
  }, []);

  const showSessionScrollbar = () => {
    if (scrollbarHideTimerRef.current !== null) {
      clearTimeout(scrollbarHideTimerRef.current);
      scrollbarHideTimerRef.current = null;
    }
    setScrollbarVisible(true);
  };

  const scheduleSessionScrollbarHide = () => {
    if (scrollbarHideTimerRef.current !== null) clearTimeout(scrollbarHideTimerRef.current);
    // 移出后保留短暂视觉提示，避免用户刚离开列表时滚动位置突然失去参照。
    scrollbarHideTimerRef.current = setTimeout(() => {
      setScrollbarVisible(false);
      scrollbarHideTimerRef.current = null;
    }, 3_000);
  };

  useEffect(() => {
    if (!menuSessionId) return;
    const closeMenuOutside = (event: PointerEvent) => {
      const owner = event.target instanceof Element
        ? event.target.closest<HTMLElement>("[data-session-menu-owner]")
        : null;
      if (owner?.dataset.sessionMenuOwner !== menuSessionId) setMenuSessionId("");
    };
    // 使用捕获阶段，保证点击会话切换等控件时先收起菜单，再执行目标控件自己的动作。
    document.addEventListener("pointerdown", closeMenuOutside, true);
    return () => document.removeEventListener("pointerdown", closeMenuOutside, true);
  }, [menuSessionId]);

  const beginRename = (session: SessionSummary) => {
    setMenuSessionId("");
    setEditingSessionId(session.key);
    setTitleDraft(session.title || session.first_message_content || "未命名会话");
  };

  const commitRename = async (session: SessionSummary) => {
    const title = titleDraft.trim();
    const original = session.title || session.first_message_content || "未命名会话";
    if (!title || title === original) {
      setEditingSessionId("");
      return;
    }
    setEditingSessionId("");
    await props.onRename(session.key, title).catch(() => setEditingSessionId(session.key));
  };

  return (
    <div className="session-panel">
      <div className="brand-lockup">
        <span className="brand-mark">B</span>
        <strong>BeanAgent</strong>
      </div>
      <button className="new-chat-button" onClick={props.onCreate}><MessageSquarePlus size={17} />新建会话</button>
      <nav
        ref={sessionListRef}
        className={`session-list ${scrollbarVisible ? "scrollbar-visible" : ""}`}
        aria-label="会话列表"
        onPointerEnter={showSessionScrollbar}
        onPointerLeave={scheduleSessionScrollbarHide}
      >
        {props.sessions.length === 0 ? <p className="session-empty">完成第一轮对话后，会话会出现在这里。</p> : groups.map((group) => (
          <section className="session-group" key={group.label}>
            <h2 className="session-group-title">{group.label}</h2>
            {group.sessions.map((session) => {
              const title = session.title || session.first_message_content || "未命名会话";
              const active = session.key === props.activeSessionId;
              return (
                <div
                  key={session.key}
                  className={`session-row ${active ? "active" : ""}`}
                  data-session-menu-owner={session.key}
                >
                  {editingSessionId === session.key ? (
                    <input
                      className="session-title-input"
                      aria-label="会话标题"
                      autoFocus
                      maxLength={60}
                      value={titleDraft}
                      onBlur={() => void commitRename(session)}
                      onChange={(event) => setTitleDraft(event.target.value)}
                      onKeyDown={(event) => {
                        if (event.key === "Enter") void commitRename(session);
                        if (event.key === "Escape") setEditingSessionId("");
                      }}
                    />
                  ) : (
                    <button className="session-row-select" onClick={() => props.onSelect(session.key)}>
                      <span>{title}</span>
                    </button>
                  )}
                  <button
                    className="session-menu-trigger"
                    aria-label={`打开会话“${title}”的菜单`}
                    aria-expanded={menuSessionId === session.key}
                    onClick={() => setMenuSessionId((current) => current === session.key ? "" : session.key)}
                  >
                    <MoreHorizontal size={17} />
                  </button>
                  {menuSessionId === session.key ? (
                    <div className="session-menu" role="menu">
                      <button role="menuitem" onClick={() => { setMenuSessionId(""); setProactiveTarget(session); }}><Bell size={15} />主动设置</button>
                      <button role="menuitem" onClick={() => beginRename(session)}><Pencil size={15} />重命名</button>
                      <button className="danger" role="menuitem" onClick={() => { setMenuSessionId(""); setDeleteTarget(session); }}><Trash2 size={15} />删除</button>
                    </div>
                  ) : null}
                </div>
              );
            })}
          </section>
        ))}
      </nav>
      <Dialog.Root open={deleteTarget !== null} onOpenChange={(open) => { if (!open && !deleting) setDeleteTarget(null); }}>
        <Dialog.Portal>
          <Dialog.Overlay className="dialog-overlay" onClick={() => { if (!deleting) setDeleteTarget(null); }} />
          <Dialog.Content className="delete-session-dialog">
            <Dialog.Title>删除“{deleteTarget ? (deleteTarget.title || deleteTarget.first_message_content || "未命名会话") : ""}”？</Dialog.Title>
            <Dialog.Description>
              该会话中的消息和工具执行记录将被永久删除。<br />
              已沉淀的长期记忆不会随会话删除。<br />
              此操作无法撤销。
            </Dialog.Description>
            <div className="delete-dialog-actions">
              <Dialog.Close asChild><button disabled={deleting}>取消</button></Dialog.Close>
              <button
                className="confirm-delete"
                disabled={deleting}
                onClick={() => {
                  if (!deleteTarget) return;
                  setDeleting(true);
                  void props.onDelete(deleteTarget.key)
                    .then(() => setDeleteTarget(null))
                    .catch(() => undefined)
                    .finally(() => setDeleting(false));
                }}
              >确认删除</button>
            </div>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>
      <ProactiveSettingsDialog target={proactiveTarget} onClose={() => setProactiveTarget(null)} />
    </div>
  );
}

function ProactiveSettingsDialog({ target, onClose }: { target: SessionSummary | null; onClose: () => void }) {
  const [settings, setSettings] = useState<ProactiveSettings | null>(null);
  const [reminders, setReminders] = useState<ScheduledReminder[]>([]);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [help, setHelp] = useState("");
  const [confirmReminderId, setConfirmReminderId] = useState("");
  const [deletingReminderId, setDeletingReminderId] = useState("");

  useEffect(() => {
    if (!target) return;
    setLoading(true);
    setError("");
    setConfirmReminderId("");
    setDeletingReminderId("");
    Promise.all([fetchProactiveSettings(target.key), fetchReminders(target.key)])
      .then(([nextSettings, nextReminders]) => { setSettings(nextSettings); setReminders(nextReminders); })
      .catch((reason) => setError(reason instanceof Error ? reason.message : "加载失败"))
      .finally(() => setLoading(false));
  }, [target]);

  useEffect(() => {
    if (!help) return;
    const close = () => setHelp("");
    document.addEventListener("pointerdown", close);
    return () => document.removeEventListener("pointerdown", close);
  }, [help]);

  const update = <K extends keyof ProactiveSettings>(key: K, value: ProactiveSettings[K]) => {
    setSettings((current) => current ? { ...current, [key]: value } : current);
  };
  const save = async () => {
    if (!target || !settings) return;
    setSaving(true);
    setError("");
    try {
      setSettings(await saveProactiveSettings(target.key, settings));
      onClose();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "保存失败");
    } finally {
      setSaving(false);
    }
  };
  const refreshReminderList = async () => {
    if (target) setReminders(await fetchReminders(target.key));
  };
  const removeReminder = async (item: ScheduledReminder) => {
    if (!target || deletingReminderId) return;
    setDeletingReminderId(item.id);
    setError("");
    try {
      await deleteReminder(target.key, item.id);
      setConfirmReminderId("");
      await refreshReminderList();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setDeletingReminderId("");
    }
  };

  return (
    <Dialog.Root open={target !== null} onOpenChange={(open) => { if (!open && !saving) onClose(); }}>
      <Dialog.Portal>
        <Dialog.Overlay className="dialog-overlay" />
        <Dialog.Content className="proactive-dialog">
          <header className="proactive-dialog-header">
            <div><Dialog.Title>主动设置</Dialog.Title><Dialog.Description>{target?.title || target?.first_message_content || "当前会话"}</Dialog.Description></div>
            <Dialog.Close asChild><button className="icon-button" aria-label="关闭"><X size={17} /></button></Dialog.Close>
          </header>
          {loading || !settings ? <div className="proactive-loading">{error || "正在加载…"}</div> : (
            <div className="proactive-dialog-body">
              <SettingsSection title="提醒" enabled={settings.reminders_enabled} onEnabled={(value) => update("reminders_enabled", value)}>
                <SettingRow label="勿扰时段处理" helpId="reminder-policy" help={help} onHelp={setHelp} helpText="延后：勿扰结束后发送；照常发送：仍按原时间；跳过：本次不发送。">
                  <select value={settings.reminder_quiet_policy} onChange={(event) => update("reminder_quiet_policy", event.target.value as ProactiveSettings["reminder_quiet_policy"])}>
                    <option value="delay">延后发送</option><option value="send">照常发送</option><option value="skip">跳过本次</option>
                  </select>
                </SettingRow>
                <div className="reminder-list">
                  <div className="reminder-list-title"><span>已创建的提醒</span><small>通过对话创建</small></div>
                  {reminders.length === 0 ? <p className="reminder-empty">暂无提醒</p> : reminders.map((item) => (
                    <div className={`reminder-item${confirmReminderId === item.id ? " confirming" : ""}`} key={item.id}>
                      <div><strong>{item.name || (item.tier === "instant" ? "固定提醒" : "AI 定时任务")}</strong><small>{item.trigger === "every" ? "周期 · " : ""}{new Date(item.fire_at).toLocaleString()} · {item.tier === "instant" ? "固定文本" : "到期执行 prompt"}{item.status === "failed" ? ` · 失败：${item.last_error}` : ""}</small></div>
                      {confirmReminderId === item.id ? (
                        <span className="reminder-confirm-actions">
                          <button type="button" disabled={deletingReminderId === item.id} onClick={() => setConfirmReminderId("")}>取消</button>
                          <button type="button" className="confirm-delete" disabled={deletingReminderId === item.id} onClick={() => void removeReminder(item)}>{deletingReminderId === item.id ? "删除中" : "确认删除"}</button>
                        </span>
                      ) : (
                        <button className="icon-button danger" aria-label="删除提醒" onClick={() => setConfirmReminderId(item.id)}><Trash2 size={15} /></button>
                      )}
                    </div>
                  ))}
                </div>
              </SettingsSection>

              <SettingsSection title="主动聊天" enabled={settings.conversation_enabled} onEnabled={(value) => update("conversation_enabled", value)}>
                <SettingRow label="主动程度" helpId="activity" help={help} onHelp={setHelp} helpText="算法倾向：克制更少尝试，均衡适合日常，积极更愿意延续明确未完成的话题；所有档位仍受间隔、次数和勿扰限制。">
                  <select value={settings.activity_level} onChange={(event) => update("activity_level", event.target.value as ProactiveSettings["activity_level"])}>
                    <option value="restrained">克制</option><option value="balanced">均衡</option><option value="active">积极</option>
                  </select>
                </SettingRow>
                <SettingRow label="最短间隔" helpId="interval" help={help} onHelp={setHelp} helpText="一次主动聊天后，至少等待这么久再尝试。这是明确的频率边界，主动程度不会越过它。">
                  <NumberSetting value={settings.min_conversation_interval_hours} min={1} max={168} suffix="小时" onChange={(value) => update("min_conversation_interval_hours", value)} />
                </SettingRow>
                <SettingRow label="每日最多" helpId="daily" help={help} onHelp={setHelp} helpText="当天最多主动聊天的次数，可输入 1 到 20。普通问题的正常回答不计入。">
                  <NumberSetting value={settings.daily_conversation_limit} min={1} max={20} suffix="次" onChange={(value) => update("daily_conversation_limit", value)} />
                </SettingRow>
              </SettingsSection>

              <section className="settings-section quiet-section">
                <div className="settings-section-title"><div><strong>勿扰时间</strong><span>提醒和主动聊天共用</span></div><Toggle checked={settings.quiet_hours_enabled} onChange={(value) => update("quiet_hours_enabled", value)} /></div>
                <div className="quiet-time-row"><input type="time" value={settings.quiet_start} onChange={(event) => update("quiet_start", event.target.value)} /><span>至</span><input type="time" value={settings.quiet_end} onChange={(event) => update("quiet_end", event.target.value)} /></div>
              </section>
              {error ? <div className="settings-error" role="alert">{error}</div> : null}
            </div>
          )}
          <footer className="proactive-dialog-footer"><Dialog.Close asChild><button className="secondary-action" disabled={saving}>取消</button></Dialog.Close><button className="primary-action" disabled={saving || loading || !settings} onClick={() => void save()}>{saving ? "保存中…" : "保存设置"}</button></footer>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

function SettingsSection({ title, enabled, onEnabled, children }: { title: string; enabled: boolean; onEnabled: (value: boolean) => void; children: ReactNode }) {
  return <section className={`settings-section${enabled ? "" : " disabled"}`}><div className="settings-section-title"><strong>{title}</strong><Toggle checked={enabled} onChange={onEnabled} /></div><div className="settings-section-content">{children}</div></section>;
}

function SettingRow({ label, helpId, help, onHelp, helpText, children }: { label: string; helpId: string; help: string; onHelp: (id: string) => void; helpText: string; children: ReactNode }) {
  return <div className="setting-row"><div className="setting-label"><span>{label}</span><span className="help-owner" onPointerDown={(event) => event.stopPropagation()}><button className="help-button" aria-label={`说明：${label}`} onClick={() => onHelp(help === helpId ? "" : helpId)}><AlertCircle size={13} /></button>{help === helpId ? <span className="help-popover">{helpText}</span> : null}</span></div>{children}</div>;
}

function Toggle({ checked, onChange }: { checked: boolean; onChange: (value: boolean) => void }) {
  return <button type="button" className={`toggle${checked ? " on" : ""}`} role="switch" aria-checked={checked} onClick={() => onChange(!checked)}><span /></button>;
}

function NumberSetting({ value, min, max, suffix, onChange }: { value: number; min: number; max: number; suffix: string; onChange: (value: number) => void }) {
  return <label className="number-setting"><input type="number" min={min} max={max} value={value} onChange={(event) => onChange(Math.max(min, Math.min(max, Number(event.target.value) || min)))} /><span>{suffix}</span></label>;
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

function MessageView({ message, navigationTurnId }: { message: ChatMessage; navigationTurnId: string }) {
  const isUser = message.role === "user";
  const parsed = useMemo(() => parseMemoryCitations(message.content), [message.content]);
  // Streamdown 在 isAnimating 期间会禁用复制和全屏按钮。Mermaid fence 一旦
  // 闭合就已经具备稳定源码，应立即放开查看大图，而不必等待 final 帧。
  const markdownAnimating = Boolean(message.streaming && !containsClosedMermaidFence(message.content));

  return (
    <article
      className={`message ${isUser ? "user-message" : "assistant-message"}`}
      data-turn-anchor={isUser ? navigationTurnId : undefined}
    >
      <div className="message-label">{isUser ? "你" : "BeanAgent"}</div>
      <div className="message-body">
        {!isUser && message.source ? <MessageSourceBadge message={message} /> : null}
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

function MessageSourceBadge({ message }: { message: ChatMessage }) {
  const details = message.source === "scheduled_reminder"
    ? { label: "提醒", icon: <Bell size={13} /> }
    : message.source === "scheduled_soft"
      ? { label: "定时任务", icon: <Wrench size={13} /> }
      : { label: "主动聊天", icon: <MessageSquarePlus size={13} /> };
  return (
    <div className={`message-source ${message.source}`}>
      <span>{details.icon}{details.label}</span>
      {message.scheduledAt ? <time>原定时间：{new Date(message.scheduledAt).toLocaleString()}</time> : null}
    </div>
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
  input: string; files: File[]; active: boolean; turnStatus: "idle" | "submitting" | "queued" | "running"; queuePosition: number | null; connected: boolean; sending: boolean; sessionId: string;
  onInput: (value: string) => void; onFiles: (files: File[]) => void; onSend: () => void; onStop: () => void;
}) {
  const [attachmentError, setAttachmentError] = useState("");
  const [dragging, setDragging] = useState(false);
  const [listening, setListening] = useState(false);
  const [speechToast, setSpeechToast] = useState("");
  const recognitionRef = useRef<BrowserSpeechRecognition | null>(null);
  const speechActiveRef = useRef(false);
  const speechHadTextRef = useRef(false);
  const speechRestartTimerRef = useRef<number | null>(null);
  const speechToastTimerRef = useRef<number | null>(null);
  const speechUpdatingRef = useRef(false);
  const speechBaseTextRef = useRef("");
  const speechFinalTextRef = useRef("");
  const onInputRef = useRef(props.onInput);
  const previews = useMemo(() => props.files.map((file) => ({ file, url: file.type.startsWith("image/") ? URL.createObjectURL(file) : "" })), [props.files]);
  const speechSupported = typeof window !== "undefined" && getSpeechRecognitionConstructor() !== null;
  useEffect(() => () => previews.forEach((item) => item.url && URL.revokeObjectURL(item.url)), [previews]);
  useEffect(() => { onInputRef.current = props.onInput; }, [props.onInput]);

  const showSpeechToast = useCallback((message: string) => {
    setSpeechToast(message);
    if (speechToastTimerRef.current !== null) window.clearTimeout(speechToastTimerRef.current);
    speechToastTimerRef.current = window.setTimeout(() => {
      speechToastTimerRef.current = null;
      setSpeechToast("");
    }, 2400);
  }, []);

  const stopSpeechInput = useCallback((abort = false, notifyNoText = false) => {
    const shouldNotifyNoText = notifyNoText && !speechHadTextRef.current;
    speechActiveRef.current = false;
    if (speechRestartTimerRef.current !== null) {
      window.clearTimeout(speechRestartTimerRef.current);
      speechRestartTimerRef.current = null;
    }
    const recognition = recognitionRef.current;
    recognitionRef.current = null;
    setListening(false);
    speechFinalTextRef.current = "";
    speechHadTextRef.current = false;
    if (shouldNotifyNoText) showSpeechToast("未识别到文字");
    if (!recognition) return;
    recognition.onstart = null;
    recognition.onend = null;
    recognition.onerror = null;
    recognition.onresult = null;
    try {
      if (abort) recognition.abort();
      else recognition.stop();
    } catch {
      // 浏览器语音识别对象可能已经结束；这里保持输入框状态收敛即可。
    }
  }, [showSpeechToast]);

  useEffect(() => () => {
    stopSpeechInput(true);
    if (speechToastTimerRef.current !== null) window.clearTimeout(speechToastTimerRef.current);
  }, [stopSpeechInput]);
  useEffect(() => {
    setSpeechToast("");
    if (!speechActiveRef.current) return;
    // 切换会话不关闭浏览器语音识别，只清空当前输入框并让后续转写从新会话重新拼接。
    speechBaseTextRef.current = "";
    speechFinalTextRef.current = "";
    speechHadTextRef.current = false;
    onInputRef.current("");
  }, [props.sessionId]);

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

  const toggleSpeechInput = useCallback(() => {
    if (!props.connected) return;
    if (listening || speechActiveRef.current) {
      stopSpeechInput(false, true);
      return;
    }
    const Recognition = getSpeechRecognitionConstructor();
    if (!Recognition) {
      showSpeechToast("当前浏览器不支持语音输入");
      return;
    }
    const recognition = new Recognition();
    recognition.lang = "zh-CN";
    recognition.continuous = true;
    recognition.interimResults = true;
    speechBaseTextRef.current = props.input;
    speechFinalTextRef.current = "";
    speechHadTextRef.current = false;
    speechActiveRef.current = true;
    recognitionRef.current = recognition;
    setSpeechToast("");
    recognition.onstart = () => {
      setListening(true);
    };
    recognition.onresult = (event) => {
      let newlyFinalText = "";
      let interimText = "";
      for (let index = event.resultIndex; index < event.results.length; index += 1) {
        const result = event.results[index];
        const text = result[0]?.transcript ?? "";
        if (result.isFinal) newlyFinalText += text;
        else interimText += text;
      }
      speechFinalTextRef.current += newlyFinalText;
      const originalText = speechBaseTextRef.current;
      speechUpdatingRef.current = true;
      const transcript = `${speechFinalTextRef.current}${interimText}`;
      const separator = originalText.trim() && transcript.trim() ? " " : "";
      if (transcript.trim()) speechHadTextRef.current = true;
      onInputRef.current(`${originalText}${separator}${transcript}`.trimStart());
      queueMicrotask(() => { speechUpdatingRef.current = false; });
    };
    recognition.onerror = (event) => {
      const error = event.error ?? "";
      if (error === "aborted") return;
      if (error === "no-speech") {
        return;
      } else if (error === "not-allowed" || error === "service-not-allowed") {
        speechActiveRef.current = false;
        showSpeechToast("浏览器没有麦克风权限");
      } else if (error === "audio-capture") {
        speechActiveRef.current = false;
        showSpeechToast("没有检测到可用麦克风");
      } else if (error === "network") {
        speechActiveRef.current = false;
        showSpeechToast("浏览器语音服务网络不可用");
      } else {
        speechActiveRef.current = false;
        showSpeechToast(`语音输入暂时不可用${error ? `：${error}` : ""}`);
      }
    };
    recognition.onend = () => {
      setListening(false);
      if (!speechActiveRef.current) {
        recognitionRef.current = null;
        return;
      }
      if (speechRestartTimerRef.current !== null) window.clearTimeout(speechRestartTimerRef.current);
      speechRestartTimerRef.current = window.setTimeout(() => {
        speechRestartTimerRef.current = null;
        if (!speechActiveRef.current || recognitionRef.current !== recognition) return;
        try {
          recognition.start();
        } catch {
          speechActiveRef.current = false;
          recognitionRef.current = null;
          setListening(false);
          showSpeechToast("语音输入重启失败");
        }
      }, 250);
    };
    try {
      recognition.start();
    } catch {
      speechActiveRef.current = false;
      setListening(false);
      recognitionRef.current = null;
      showSpeechToast("语音输入启动失败");
    }
  }, [listening, props.connected, props.input, showSpeechToast, stopSpeechInput]);

  const handleInputChange = useCallback((value: string) => {
    if ((listening || speechActiveRef.current) && !speechUpdatingRef.current) {
      speechBaseTextRef.current = value;
      speechFinalTextRef.current = "";
      speechHadTextRef.current = false;
    }
    props.onInput(value);
  }, [listening, props.onInput]);

  const handleSend = useCallback(() => {
    stopSpeechInput();
    props.onSend();
  }, [props.onSend, stopSpeechInput]);

  const handleStop = useCallback(() => {
    props.onStop();
  }, [props.onStop]);
  const speechRunning = listening || speechActiveRef.current;

  return (
    <>
      {speechToast && typeof document !== "undefined" ? createPortal((
        <div className="speech-toast" role="status">
          <AlertCircle size={20} /><span>{speechToast}</span>
        </div>
      ), document.body) : null}
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
          onChange={(event) => handleInputChange(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey && !props.active) { event.preventDefault(); handleSend(); }
          }}
          placeholder={props.connected ? "输入消息，或附加文本与图片" : "等待连接恢复"}
          disabled={!props.connected}
          rows={2}
        />
        <div className="composer-actions">
          <label className="icon-button composer-tool-button attach-button" title="添加文本或图片">
            <Paperclip size={18} /><span className="sr-only">添加附件</span>
            <input type="file" multiple accept={ATTACHMENT_ACCEPT} onChange={(event) => { addFiles(Array.from(event.target.files ?? [])); event.target.value = ""; }} />
          </label>
          <button
            type="button"
            className={`icon-button composer-tool-button voice-button${speechRunning ? " listening" : ""}`}
            title={speechSupported ? (speechRunning ? "停止语音输入" : "语音输入") : "当前浏览器不支持语音输入"}
            aria-label={speechRunning ? "停止语音输入" : "语音输入"}
            aria-pressed={speechRunning}
            disabled={!speechSupported || (!props.connected && !speechRunning)}
            onClick={toggleSpeechInput}
          >
            {speechRunning ? (
              <span className="voice-levels" aria-hidden="true">
                <span /><span /><span /><span />
              </span>
            ) : <Mic size={19} />}
          </button>
          {props.turnStatus === "queued" ? (
            <span className="queue-status" role="status">
              {props.queuePosition === 1 ? "排队中 · 即将开始" : `排队中 · 前面还有 ${(props.queuePosition ?? 1) - 1} 个会话`}
            </span>
          ) : null}
          {props.turnStatus === "submitting" ? <span className="queue-status" role="status">正在提交...</span> : null}
          {props.active ? (
            <button className="send-button stop" aria-label="停止" onClick={handleStop}><CircleStop size={18} /><span>停止</span></button>
          ) : (
            <button className="send-button" aria-label="发送" onClick={handleSend} disabled={props.sending || (!props.input.trim() && props.files.length === 0)}><SendHorizontal size={18} /><span>发送</span></button>
          )}
        </div>
      </div>
      </footer>
    </>
  );
}

function EmptyConversation() {
  return (
    <div className="empty-conversation">
      <span className="empty-mark">B</span>
      <h1>从一个具体问题开始对话</h1>
    </div>
  );
}

function ConversationSkeleton() {
  return (
    <div className="conversation-skeleton" aria-hidden="true">
      <div className="skeleton-line short" />
      <div className="skeleton-block" />
      <div className="skeleton-line" />
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

function readStoredSession(): string {
  const stored = localStorage.getItem(SESSION_STORAGE_KEY)?.trim() ?? "";
  if (!stored) return "";
  if (stored.startsWith("web:") && stored.slice(4)) return stored;
  if (!stored.includes(":")) return `web:${stored}`;
  return "";
}

function upsertSessionSummary(current: SessionSummary[], incoming: SessionSummary): SessionSummary[] {
  const index = current.findIndex((session) => session.key === incoming.key);
  if (index < 0) return [incoming, ...current];
  const next = [...current];
  next[index] = { ...current[index], ...incoming };
  return next;
}

function mergeNavigationTurns(globalTurns: TurnNavigationEntry[], messageTurns: TurnNavigationEntry[]): TurnNavigationEntry[] {
  if (!globalTurns.length) {
    return messageTurns.map((turn, index) => ({ ...turn, turnIndex: turn.turnIndex ?? index + 1 }));
  }
  const usedIds = new Set(globalTurns.map((turn) => turn.id));
  const usedSeqs = new Set(globalTurns.map((turn) => turn.seq).filter((seq): seq is number => typeof seq === "number"));
  const usedQuestions = new Set(globalTurns.map((turn) => turn.question.trim()).filter(Boolean));
  const next = [...globalTurns];
  for (const turn of messageTurns) {
    if (usedIds.has(turn.id) || (typeof turn.seq === "number" && usedSeqs.has(turn.seq))) continue;
    if (typeof turn.seq !== "number" && usedQuestions.has(turn.question.trim())) continue;
    next.push({ ...turn, turnIndex: turn.turnIndex ?? next.length + 1 });
  }
  return next;
}

function dedupeMessages(messages: ChatMessage[]): ChatMessage[] {
  const seen = new Set<string>();
  const next: ChatMessage[] = [];
  for (const message of messages) {
    if (seen.has(message.id)) continue;
    seen.add(message.id);
    next.push(message);
  }
  return next;
}

function upsertMessage(messages: ChatMessage[], incoming: ChatMessage): ChatMessage[] {
  const index = messages.findIndex((message) => message.id === incoming.id);
  if (index < 0) return [...messages, incoming];
  const next = [...messages];
  next[index] = { ...next[index], ...incoming };
  return next;
}

function mergeMessageWindow(persistedMessages: ChatMessage[], notifications: ChatMessage[]): ChatMessage[] {
  return dedupeMessages(mergeTimeline(
    persistedMessages,
    notificationsForMessageWindow(persistedMessages, notifications),
  ));
}

function notificationsForMessageWindow(persistedMessages: ChatMessage[], notifications: ChatMessage[]): ChatMessage[] {
  const times = persistedMessages
    .map((message) => Date.parse(message.timestamp || ""))
    .filter((time) => !Number.isNaN(time));
  if (!times.length) return [];
  const start = Math.min(...times);
  const end = Math.max(...times);
  return notifications.filter((message) => {
    if (!isStandaloneNotification(message)) return false;
    const time = Date.parse(message.timestamp || "");
    return !Number.isNaN(time) && time >= start && time <= end;
  });
}

function isStandaloneNotification(message: ChatMessage): boolean {
  return typeof message.seq !== "number"
    && (message.source === "scheduled_reminder" || message.source === "scheduled_soft");
}

function isNotificationFinalFrame(frame: ChatFrame): frame is Extract<ChatFrame, { type: "message.final" }> {
  return frame.type === "message.final" && Boolean(frame.metadata?.notification);
}

function notificationFrameToMessage(frame: Extract<ChatFrame, { type: "message.final" }>): ChatMessage | null {
  const metadata = frame.metadata ?? {};
  const source = String(metadata.source || "");
  if (source !== "scheduled_reminder" && source !== "scheduled_soft") return null;
  const id = frame.message_id
    || String(metadata.notification_id || metadata.message_id || "");
  if (!id) return null;
  return {
    id,
    role: "assistant",
    content: frame.content,
    thinking: frame.thinking ?? "",
    media: frame.media ?? [],
    tools: [],
    streaming: false,
    source,
    scheduledAt: String(metadata.scheduled_at || "") || undefined,
    timestamp: String(metadata.generated_at || "") || undefined,
  };
}

function firstSeqFromRows(rows: MessageRow[]): number | null {
  const first = rows.find((row) => typeof row.seq === "number");
  return typeof first?.seq === "number" ? first.seq : null;
}

function shortSession(sessionId: string): string { return sessionId ? `会话 ${sessionId.slice(-8)}` : "正在创建会话"; }
function fileName(path: string): string { return path.replaceAll("\\", "/").split("/").pop() || "附件"; }
function fileSuffix(name: string): string { const index = name.lastIndexOf("."); return index >= 0 ? name.slice(index).toLowerCase() : ""; }
function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Number((bytes / 1024).toFixed(1))} KB`;
  return `${Number((bytes / 1024 / 1024).toFixed(1))} MB`;
}
