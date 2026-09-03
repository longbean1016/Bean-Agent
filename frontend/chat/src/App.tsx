import * as Collapsible from "@radix-ui/react-collapsible";
import * as Dialog from "@radix-ui/react-dialog";
import { useVirtualizer } from "@tanstack/react-virtual";
import { code } from "@streamdown/code";
import {
  AlertCircle,
  Atom,
  ArrowDown,
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
import { useCallback, useEffect, useLayoutEffect, useMemo, useReducer, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type { ComponentPropsWithoutRef, CSSProperties } from "react";
import { Streamdown } from "streamdown";
import { StickToBottom, useStickToBottomContext } from "use-stick-to-bottom";
import type { StickToBottomContext } from "use-stick-to-bottom";

import { deleteSession, deleteWorkspace, fetchMessagePage, fetchMessagesAroundPage, fetchNotifications, fetchOlderMessages, fetchSessions, fetchTurns, fetchWorkspaces, mediaUrl, registerWorkspace, renameSession, uploadAttachment } from "./api";
import { idleTurnState, initialChatState, notificationRowsToMessages, reduceChatFrame, rowsToMessages } from "./chatReducer";
import { composeTimeline, reconcileMessages } from "./timeline";
import { parseMemoryCitations } from "./citations";
import type { MemoryCitation } from "./citations";
import { MermaidBlock } from "./MermaidBlock";
import { ApprovalPanel, PermissionSelector, WorkspaceSelector } from "./SandboxControls";
import { SessionSidebar } from "./SessionSidebar";
import { pathForSession, routeKey, sessionFromPath } from "./chatRoute";
import type { ApprovalRequest, ChatFrame, ChatMessage, ConnectionStatus, ContextUsage, MessageRow, SandboxMode, SandboxSnapshot, SessionSummary, SessionUsage, ToolActivity, TurnNavigationEntry, Workspace } from "./types";
import { groupMessagesIntoNavigationTurns, TurnNavigator, turnsFromMessages } from "./TurnNavigator";
import { BeanWebSocketClient } from "./websocketClient";

const SESSION_STORAGE_KEY = "beanagent.session_id";
const THEME_STORAGE_KEY = "beanagent.theme";
const MAX_ATTACHMENTS = 8;
const MAX_TEXT_ATTACHMENT_SIZE = 2 * 1024 * 1024;
const MAX_IMAGE_ATTACHMENT_SIZE = 10 * 1024 * 1024;
const TURN_CONTEXT_BEFORE_MESSAGES = 20;
const COMPACTION_NOTICE_MIN_MS = 900;
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
type MessageWindowState = {
  hasMoreBefore: boolean;
  nextBeforeSeq: number | null;
  hasMoreAfter: boolean;
  nextAfterSeq: number | null;
  hasTailWindow: boolean;
};
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
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [sandboxBySession, setSandboxBySession] = useState<Record<string, SandboxSnapshot>>({});
  const [pendingApprovals, setPendingApprovals] = useState<Record<string, ApprovalRequest[]>>({});
  const [approvalDecisionRequests, setApprovalDecisionRequests] = useState<Record<string, string>>({});
  const [sandboxRequest, setSandboxRequest] = useState<{ id: string; sessionId: string } | null>(null);
  const [newSessionWorkspaceId, setNewSessionWorkspaceId] = useState<string | null>(null);
  const [newSessionMode, setNewSessionMode] = useState<SandboxMode>("read-only");
  const [turnsBySession, setTurnsBySession] = useState<Record<string, TurnNavigationEntry[]>>({});
  const [requestedTurnId, setRequestedTurnId] = useState("");
  const [notificationsBySession, setNotificationsBySession] = useState<Record<string, ChatMessage[]>>({});
  const [messageWindows, setMessageWindows] = useState<Record<string, MessageWindowState>>({});
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
  const [compactionNotice, setCompactionNotice] = useState({ sessionId: "", visible: false });
  const clientRef = useRef<BeanWebSocketClient | null>(null);
  const stickToBottomRef = useRef<StickToBottomContext | null>(null);
  const chatRef = useRef(chat);
  const messageWindowsRef = useRef(messageWindows);
  const loadingMessageWindowRef = useRef("");
  const compactionStartedAtRef = useRef<Record<string, number>>({});
  const compactionNoticeTimersRef = useRef<Record<string, ReturnType<typeof setTimeout>>>({});
  const routeSessionRef = useRef(routeSession);
  const workspacesRef = useRef(workspaces);
  const pendingApprovalsRef = useRef(pendingApprovals);
  const approvalDecisionRequestsRef = useRef(approvalDecisionRequests);
  const sandboxRequestRef = useRef(sandboxRequest);
  const newSessionConfigRef = useRef<{ workspaceId: string | null; mode: SandboxMode }>({
    workspaceId: null,
    mode: "read-only",
  });
  const sessionLoadVersionsRef = useRef<Record<string, number>>({});
  const reloadSessionRef = useRef<(sessionId: string) => void>(() => undefined);
  chatRef.current = chat;
  messageWindowsRef.current = messageWindows;
  routeSessionRef.current = routeSession;
  workspacesRef.current = workspaces;
  pendingApprovalsRef.current = pendingApprovals;
  approvalDecisionRequestsRef.current = approvalDecisionRequests;
  sandboxRequestRef.current = sandboxRequest;
  newSessionConfigRef.current = { workspaceId: newSessionWorkspaceId, mode: newSessionMode };

  useEffect(() => () => {
    for (const timer of Object.values(compactionNoticeTimersRef.current)) clearTimeout(timer);
  }, []);

  const currentTurn = chat.turnStates[chat.sessionId] ?? idleTurnState;
  const currentContextUsage = chat.contextUsage[chat.sessionId];
  const currentSessionUsage = chat.sessionUsage[chat.sessionId];
  const currentSessionSummary = sessions.find((session) => session.key === chat.sessionId);
  const currentSandbox = sandboxBySession[chat.sessionId];
  const currentWorkspaceId = chat.sessionId
    ? (currentSandbox?.workspace_id ?? currentSessionSummary?.workspace_id ?? null)
    : newSessionWorkspaceId;
  const currentSandboxMode = chat.sessionId
    ? (currentSandbox?.sandbox_mode ?? currentSessionSummary?.sandbox_mode ?? "read-only")
    : newSessionMode;
  const currentWorkspaceValid = chat.sessionId
    ? (currentSandbox?.workspace_valid ?? currentSessionSummary?.workspace_valid ?? true)
    : (workspaces.find((workspace) => workspace.id === newSessionWorkspaceId)?.valid ?? true);
  const currentApproval = pendingApprovals[chat.sessionId]?.[0];
  const turnActive = currentTurn.status === "submitting" || currentTurn.status === "queued" || currentTurn.status === "running" || currentTurn.status === "compacting";
  const restoringSession = Boolean(chat.sessionId && loadingSessionId === chat.sessionId && chat.messages.length === 0);
  const displayMessages = useMemo(() => composeTimeline(
    messageWindows[chat.sessionId]?.hasTailWindow === false
      ? chat.messages.filter((message) => typeof message.seq === "number" && message.seq >= 0)
      : chat.messages,
    notificationsBySession[chat.sessionId] ?? [],
    Boolean(messageWindows[chat.sessionId]?.hasTailWindow ?? true),
  ), [chat.messages, chat.sessionId, messageWindows, notificationsBySession]);
  const messageTurns = useMemo(() => turnsFromMessages(displayMessages), [displayMessages]);
  const conversationTurns = useMemo(() => (
    mergeNavigationTurns(turnsBySession[chat.sessionId] ?? [], messageTurns)
  ), [chat.sessionId, messageTurns, turnsBySession]);
  const conversationTurnGroups = useMemo(() => groupMessagesIntoNavigationTurns(displayMessages), [displayMessages]);

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

  const refreshWorkspaces = useCallback(async () => {
    try {
      setWorkspaces(await fetchWorkspaces());
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
    setNotificationsBySession((current) => ({
      ...current,
      [sessionId]: upsertMessage(current[sessionId] ?? [], notification),
    }));
    return true;
  }, []);

  const clearApprovalsForSession = useCallback((sessionId: string) => {
    const approvalIds = new Set(
      (pendingApprovalsRef.current[sessionId] ?? []).map((approval) => approval.id),
    );
    const nextApprovals = { ...pendingApprovalsRef.current, [sessionId]: [] };
    pendingApprovalsRef.current = nextApprovals;
    setPendingApprovals(nextApprovals);
    if (!approvalIds.size) return;
    const nextRequests = Object.fromEntries(
      Object.entries(approvalDecisionRequestsRef.current)
        .filter(([approvalId]) => !approvalIds.has(approvalId)),
    );
    approvalDecisionRequestsRef.current = nextRequests;
    setApprovalDecisionRequests(nextRequests);
  }, []);

  const handleFrame = useCallback((frame: ChatFrame) => {
    if (handleNotificationFrame(frame)) return;
    dispatch(frame);
    if (frame.type === "session.subscribed") {
      // 服务端会在订阅确认后重放当前 pending；先丢弃旧缓存，避免重连产生重复审批卡片。
      clearApprovalsForSession(frame.session_id);
    }
    if (frame.type === "sandbox.updated") {
      const snapshot = frame.sandbox;
      setSandboxBySession((current) => ({ ...current, [snapshot.session_id]: snapshot }));
      setSessions((current) => current.map((session) => session.key === snapshot.session_id ? {
        ...session,
        workspace_id: snapshot.workspace_id,
        cwd_snapshot: snapshot.cwd_snapshot,
        workspace_title: snapshot.workspace_title,
        workspace_path: snapshot.workspace_path,
        workspace_valid: snapshot.workspace_valid,
        sandbox_mode: snapshot.sandbox_mode,
      } : session));
      if (frame.request_id && sandboxRequestRef.current?.id === frame.request_id) {
        sandboxRequestRef.current = null;
        setSandboxRequest(null);
      }
    }
    if (frame.type === "approval.requested") {
      const existing = pendingApprovalsRef.current[frame.session_id] ?? [];
      const nextApprovals = {
        ...pendingApprovalsRef.current,
        [frame.session_id]: existing.some((item) => item.id === frame.approval.id)
          ? existing.map((item) => item.id === frame.approval.id ? frame.approval : item)
          : [...existing, frame.approval],
      };
      pendingApprovalsRef.current = nextApprovals;
      setPendingApprovals(nextApprovals);
    }
    if (frame.type === "approval.resolved") {
      const nextApprovals = {
        ...pendingApprovalsRef.current,
        [frame.session_id]: (pendingApprovalsRef.current[frame.session_id] ?? [])
          .filter((item) => item.id !== frame.approval_id),
      };
      pendingApprovalsRef.current = nextApprovals;
      setPendingApprovals(nextApprovals);
      const nextRequests = { ...approvalDecisionRequestsRef.current };
      delete nextRequests[frame.approval_id];
      approvalDecisionRequestsRef.current = nextRequests;
      setApprovalDecisionRequests(nextRequests);
    }
    if (frame.type === "error") {
      if (frame.request_id && sandboxRequestRef.current?.id === frame.request_id) {
        sandboxRequestRef.current = null;
        setSandboxRequest(null);
      }
      const approvalId = Object.entries(approvalDecisionRequestsRef.current)
        .find(([, requestId]) => requestId === frame.request_id)?.[0];
      if (approvalId) {
        const nextRequests = { ...approvalDecisionRequestsRef.current };
        delete nextRequests[approvalId];
        approvalDecisionRequestsRef.current = nextRequests;
        setApprovalDecisionRequests(nextRequests);
      }
    }
    if (frame.type === "context.compaction.started") {
      compactionStartedAtRef.current[frame.session_id] = Date.now();
      const previousTimer = compactionNoticeTimersRef.current[frame.session_id];
      if (previousTimer) clearTimeout(previousTimer);
      if (frame.session_id === routeSessionRef.current || frame.session_id === chatRef.current.sessionId) {
        setCompactionNotice({ sessionId: frame.session_id, visible: true });
      }
    }
    if (frame.type === "context.compaction.completed" || frame.type === "context.compaction.failed") {
      const startedAt = compactionStartedAtRef.current[frame.session_id] ?? Date.now();
      const remaining = Math.max(0, COMPACTION_NOTICE_MIN_MS - (Date.now() - startedAt));
      const hide = () => {
        delete compactionStartedAtRef.current[frame.session_id];
        delete compactionNoticeTimersRef.current[frame.session_id];
        setCompactionNotice((current) => current.sessionId === frame.session_id
          ? { ...current, visible: false }
          : current);
      };
      if (remaining > 0) {
        compactionNoticeTimersRef.current[frame.session_id] = setTimeout(hide, remaining);
      } else {
        hide();
      }
    }
    if (frame.type === "session.created") {
      localStorage.setItem(SESSION_STORAGE_KEY, frame.session_id);
      const createdAt = new Date().toISOString();
      if (!routeSessionRef.current) {
        window.history.pushState({}, "", pathForSession(frame.session_id));
        setRouteSession(frame.session_id);
      }
      setSessions((current) => current.some((session) => session.key === frame.session_id)
        ? current
        : [{
            key: frame.session_id,
            title: "新对话",
            created_at: createdAt,
            updated_at: createdAt,
            message_count: 0,
            first_message_content: "",
            workspace_id: newSessionConfigRef.current.workspaceId,
            workspace_title: workspacesRef.current.find((workspace) => workspace.id === newSessionConfigRef.current.workspaceId)?.title ?? null,
            workspace_path: workspacesRef.current.find((workspace) => workspace.id === newSessionConfigRef.current.workspaceId)?.canonical_path ?? null,
            workspace_valid: workspacesRef.current.find((workspace) => workspace.id === newSessionConfigRef.current.workspaceId)?.valid ?? false,
            sandbox_mode: newSessionConfigRef.current.mode,
          }, ...current]);
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
      // cancelled/unavailable 审批不一定另发 resolved；Turn 结束时必须让 composer 收敛。
      clearApprovalsForSession(frame.session_id);
      void refreshSessions();
      void refreshTurnPreviews(frame.session_id);
    }
    if (frame.type === "turn.interrupted") {
      // 后端发送该帧前已完成中断轮持久化。立刻用带 seq 的权威行替换本地草稿，
      // 避免连续中断时多个无 seq 草稿按客户端时间错序。
      reloadSessionRef.current(frame.session_id);
      clearApprovalsForSession(frame.session_id);
    }
  }, [clearApprovalsForSession, handleNotificationFrame, refreshSessions, refreshTurnPreviews]);

  useEffect(() => {
    const client = new BeanWebSocketClient({
      onFrame: handleFrame,
      onStatus: (status) => {
        setConnection(status);
      },
    });
    clientRef.current = client;
    client.connect();
    void refreshSessions();
    void refreshWorkspaces();
    return () => client.close();
  }, [handleFrame, refreshSessions, refreshWorkspaces]);

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
    const loadVersion = (sessionLoadVersionsRef.current[sessionId] ?? 0) + 1;
    sessionLoadVersionsRef.current[sessionId] = loadVersion;
    setLoadingSessionId(sessionId);
    try {
      const [page, notifications, turns] = await Promise.all([
        fetchMessagePage(sessionId),
        fetchNotifications(sessionId),
        fetchTurns(sessionId),
      ]);
      const rows = page.items ?? [];
      const notificationMessages = notificationRowsToMessages(notifications);
      const messages = rowsToMessages(rows);
      if (sessionLoadVersionsRef.current[sessionId] !== loadVersion) return;
      setMessageWindows((current) => ({
        ...current,
        [sessionId]: {
          hasMoreBefore: Boolean(page.has_more),
          nextBeforeSeq: page.next_before_seq ?? firstSeqFromRows(rows),
          hasMoreAfter: false,
          nextAfterSeq: null,
          hasTailWindow: true,
        },
      }));
      setNotificationsBySession((current) => ({ ...current, [sessionId]: notificationMessages }));
      setTurnsBySession((current) => ({ ...current, [sessionId]: turns }));
      if (routeSessionRef.current !== sessionId) return;
      localStorage.setItem(SESSION_STORAGE_KEY, sessionId);
      dispatch({ type: "ui.session.select", sessionId, messages });
      if (closeSidebar) setSidebarOpen(false);
    } catch (error) {
      dispatch(errorFrame(error));
    } finally {
      setLoadingSessionId((current) => current === sessionId ? "" : current);
    }
  };
  reloadSessionRef.current = (sessionId: string) => {
    void loadSession(sessionId, false);
  };

  const decideApproval = useCallback((approval: ApprovalRequest, decision: "allowed-once" | "rejected") => {
    if (approvalDecisionRequestsRef.current[approval.id]) return;
    const requestId = crypto.randomUUID();
    const nextRequests = { ...approvalDecisionRequestsRef.current, [approval.id]: requestId };
    approvalDecisionRequestsRef.current = nextRequests;
    setApprovalDecisionRequests(nextRequests);
    const sent = clientRef.current?.send({
      type: "approval.decide",
      request_id: requestId,
      session_id: approval.session_id,
      approval_id: approval.id,
      decision,
    });
    if (!sent) {
      const currentRequests = { ...approvalDecisionRequestsRef.current };
      delete currentRequests[approval.id];
      approvalDecisionRequestsRef.current = currentRequests;
      setApprovalDecisionRequests(currentRequests);
      dispatch({
        type: "error",
        request_id: requestId,
        session_id: approval.session_id,
        code: "closed",
        message: "审批结果未发送，连接已断开",
      });
      return;
    }
  }, []);

  const rejectPendingApprovals = useCallback((sessionId: string) => {
    for (const approval of pendingApprovalsRef.current[sessionId] ?? []) {
      if (!approvalDecisionRequestsRef.current[approval.id]) decideApproval(approval, "rejected");
    }
  }, [decideApproval]);

  const createSession = (workspaceId: string | null = null) => {
    if (connection !== "connected") return;
    if (chat.sessionId) rejectPendingApprovals(chat.sessionId);
    setLoadingSessionId("");
    localStorage.removeItem(SESSION_STORAGE_KEY);
    window.history.pushState({}, "", "/");
    routeSessionRef.current = "";
    setRouteSession("");
    dispatch({ type: "ui.session.select", sessionId: "", messages: [] });
    // 每次点击新建都创建全新的根页面草稿，不影响任何已有会话的独立草稿。
    setTextDrafts((current) => ({ ...current, [routeKey("")]: "" }));
    setFileDrafts((current) => ({ ...current, [routeKey("")]: [] }));
    setInput("");
    setFiles([]);
    setNewSessionWorkspaceId(workspaceId);
    setNewSessionMode("read-only");
    setSidebarOpen(false);
  };

  const selectSession = (sessionId: string) => {
    if (chat.sessionId && chat.sessionId !== sessionId) rejectPendingApprovals(chat.sessionId);
    window.history.pushState({}, "", pathForSession(sessionId));
    routeSessionRef.current = sessionId;
    setRouteSession(sessionId);
    setInput(textDrafts[routeKey(sessionId)] ?? "");
    setFiles(fileDrafts[routeKey(sessionId)] ?? []);
    void loadSession(sessionId);
  };

  const handleRegisterWorkspace = async (path: string, title: string) => {
    try {
      const workspace = await registerWorkspace(path, title);
      setWorkspaces((current) => [workspace, ...current.filter((item) => item.id !== workspace.id)]);
      return workspace;
    } catch (error) {
      dispatch(errorFrame(error));
      throw error;
    }
  };

  const handleDeleteWorkspace = async (workspaceId: string) => {
    try {
      await deleteWorkspace(workspaceId);
      setWorkspaces((current) => current.filter((workspace) => workspace.id !== workspaceId));
      setSessions((current) => current.map((session) => session.workspace_id === workspaceId ? {
        ...session,
        workspace_id: null,
        cwd_snapshot: null,
        workspace_title: null,
        workspace_path: null,
        workspace_valid: false,
        sandbox_mode: session.sandbox_mode === "workspace-write" ? "read-only" : session.sandbox_mode,
      } : session));
      setSandboxBySession((current) => Object.fromEntries(Object.entries(current).map(([sessionId, snapshot]) => (
        snapshot.workspace_id === workspaceId
          ? [sessionId, {
              ...snapshot,
              workspace_id: null,
              cwd_snapshot: null,
              workspace_title: null,
              workspace_path: null,
              workspace_valid: false,
              sandbox_mode: snapshot.sandbox_mode === "workspace-write" ? "read-only" : snapshot.sandbox_mode,
            }]
          : [sessionId, snapshot]
      ))));
      if (newSessionWorkspaceId === workspaceId) {
        setNewSessionWorkspaceId(null);
        if (newSessionMode === "workspace-write") setNewSessionMode("read-only");
      }
    } catch (error) {
      dispatch(errorFrame(error));
      throw error;
    }
  };

  const handleWorkspaceChange = (workspaceId: string | null) => {
    if (!chat.sessionId) {
      setNewSessionWorkspaceId(workspaceId);
      if (workspaceId === null && newSessionMode === "workspace-write") setNewSessionMode("read-only");
      return;
    }
    const requestId = crypto.randomUUID();
    const request = { id: requestId, sessionId: chat.sessionId };
    sandboxRequestRef.current = request;
    setSandboxRequest(request);
    const sent = clientRef.current?.send({
      type: "workspace.bind",
      request_id: requestId,
      session_id: chat.sessionId,
      workspace_id: workspaceId,
    });
    if (!sent) {
      sandboxRequestRef.current = null;
      setSandboxRequest(null);
      dispatch({ type: "error", request_id: requestId, session_id: chat.sessionId, code: "closed", message: "工作目录未更新，连接已断开" });
      return;
    }
  };

  const handleSandboxModeChange = (mode: SandboxMode, riskConfirmed = false) => {
    if (mode === "workspace-write" && currentWorkspaceId === null) return;
    if (!chat.sessionId) {
      setNewSessionMode(mode);
      return;
    }
    const requestId = crypto.randomUUID();
    const request = { id: requestId, sessionId: chat.sessionId };
    sandboxRequestRef.current = request;
    setSandboxRequest(request);
    const sent = clientRef.current?.send({
      type: "sandbox.mode.set",
      request_id: requestId,
      session_id: chat.sessionId,
      sandbox_mode: mode,
      ...(mode === "danger-full-access" ? { risk_confirmed: riskConfirmed } : {}),
    });
    if (!sent) {
      sandboxRequestRef.current = null;
      setSandboxRequest(null);
      dispatch({ type: "error", request_id: requestId, session_id: chat.sessionId, code: "closed", message: "会话权限未更新，连接已断开" });
      return;
    }
  };

  const handleTurnRequest = useCallback(async (turn: TurnNavigationEntry) => {
    const sessionId = chatRef.current.sessionId;
    if (!sessionId) return;
    stickToBottomRef.current?.stopScroll();
    setRequestedTurnId(turn.id);
    const loaded = chatRef.current.messages.some((message) => (
      message.role === "user"
      && (message.turnId === turn.id || message.id === turn.id || message.seq === turn.seq)
    ));
    if (loaded) return;
    if (typeof turn.seq !== "number") {
      setRequestedTurnId("");
      return;
    }
    try {
      const [page, notifications] = await Promise.all([
        fetchMessagesAroundPage(sessionId, Math.max(0, turn.seq - TURN_CONTEXT_BEFORE_MESSAGES)),
        fetchNotifications(sessionId),
      ]);
      const rows = page.items ?? [];
      const notificationMessages = notificationRowsToMessages(notifications);
      const currentMessages = chatRef.current.messages;
      const runtimeMessages = currentMessages.filter((message) => (
        typeof message.seq !== "number" || message.seq < 0
      ));
      const persistedMessages = reconcileMessages([...rowsToMessages(rows), ...runtimeMessages]);
      const hasTailWindow = page.has_after === false;
      setMessageWindows((current) => ({
        ...current,
        [sessionId]: {
          hasMoreBefore: Boolean(page.has_before),
          nextBeforeSeq: page.next_before_seq ?? firstSeqFromRows(rows),
          hasMoreAfter: Boolean(page.has_after),
          nextAfterSeq: page.has_after ? nextSeqFromRows(rows) : null,
          hasTailWindow,
        },
      }));
      setNotificationsBySession((current) => ({ ...current, [sessionId]: notificationMessages }));
      dispatch({ type: "ui.session.select", sessionId, messages: persistedMessages, replace: true });
    } catch (error) {
      setRequestedTurnId((current) => current === turn.id ? "" : current);
      dispatch(errorFrame(error));
    }
  }, []);

  const handleTurnPositioned = useCallback((turnId: string) => {
    setRequestedTurnId((current) => current === turnId ? "" : current);
  }, []);

  const loadOlderMessages = useCallback(async () => {
    const sessionId = chatRef.current.sessionId;
    const windowState = messageWindowsRef.current[sessionId];
    if (!sessionId || !windowState?.hasMoreBefore || typeof windowState.nextBeforeSeq !== "number") return;
    const loadingKey = `${sessionId}:before`;
    if (loadingMessageWindowRef.current) return;
    loadingMessageWindowRef.current = loadingKey;
    try {
      const page = await fetchOlderMessages(sessionId, windowState.nextBeforeSeq);
      const olderMessages = rowsToMessages(page.items ?? []);
      const persistedMessages = reconcileMessages([...olderMessages, ...chatRef.current.messages]);
      dispatch({ type: "ui.session.select", sessionId, messages: persistedMessages, replace: true });
      setMessageWindows((current) => ({
        ...current,
        [sessionId]: {
          hasMoreBefore: Boolean(page.has_more),
          nextBeforeSeq: page.next_before_seq ?? firstSeqFromRows(page.items ?? []),
          hasMoreAfter: Boolean(current[sessionId]?.hasMoreAfter),
          nextAfterSeq: current[sessionId]?.nextAfterSeq ?? null,
          hasTailWindow: Boolean(current[sessionId]?.hasTailWindow),
        },
      }));
    } catch (error) {
      dispatch(errorFrame(error));
    } finally {
      if (loadingMessageWindowRef.current === loadingKey) loadingMessageWindowRef.current = "";
    }
  }, []);

  const loadNewerMessages = useCallback(async () => {
    const sessionId = chatRef.current.sessionId;
    const windowState = messageWindowsRef.current[sessionId];
    if (!sessionId || !windowState?.hasMoreAfter || typeof windowState.nextAfterSeq !== "number") return;
    const loadingKey = `${sessionId}:after`;
    if (loadingMessageWindowRef.current) return;
    loadingMessageWindowRef.current = loadingKey;
    try {
      const page = await fetchMessagesAroundPage(sessionId, windowState.nextAfterSeq);
      const rows = page.items ?? [];
      const messages = reconcileMessages([...chatRef.current.messages, ...rowsToMessages(rows)]);
      dispatch({ type: "ui.session.select", sessionId, messages, replace: true });
      setMessageWindows((current) => ({
        ...current,
        [sessionId]: {
          hasMoreBefore: Boolean(current[sessionId]?.hasMoreBefore),
          nextBeforeSeq: current[sessionId]?.nextBeforeSeq ?? firstSeqFromRows(rows),
          hasMoreAfter: Boolean(page.has_after),
          nextAfterSeq: page.has_after ? nextSeqFromRows(rows) : null,
          hasTailWindow: page.has_after === false,
        },
      }));
    } catch (error) {
      dispatch(errorFrame(error));
    } finally {
      if (loadingMessageWindowRef.current === loadingKey) loadingMessageWindowRef.current = "";
    }
  }, []);

  const returnToLatestMessages = useCallback(async (runtimeSeed: ChatMessage[] = []) => {
    const sessionId = chatRef.current.sessionId;
    if (!sessionId) return;
    const loadingKey = `${sessionId}:tail`;
    if (loadingMessageWindowRef.current) return;
    loadingMessageWindowRef.current = loadingKey;
    setRequestedTurnId("");
    try {
      const [page, notifications] = await Promise.all([
        fetchMessagePage(sessionId),
        fetchNotifications(sessionId),
      ]);
      if (chatRef.current.sessionId !== sessionId) return;
      const rows = page.items ?? [];
      const currentRuntimeMessages = chatRef.current.messages.filter((message) => (
          typeof message.seq !== "number" || message.seq < 0
        ));
      const currentRuntimeIds = new Set(currentRuntimeMessages.map((message) => message.id));
      const runtimeMessages = reconcileMessages([
        ...currentRuntimeMessages,
        ...runtimeSeed.filter((message) => !currentRuntimeIds.has(message.id)),
      ]);
      const messages = reconcileMessages([...rowsToMessages(rows), ...runtimeMessages]);
      setNotificationsBySession((current) => ({
        ...current,
        [sessionId]: notificationRowsToMessages(notifications),
      }));
      setMessageWindows((current) => ({
        ...current,
        [sessionId]: {
          hasMoreBefore: Boolean(page.has_more),
          nextBeforeSeq: page.next_before_seq ?? firstSeqFromRows(rows),
          hasMoreAfter: false,
          nextAfterSeq: null,
          hasTailWindow: true,
        },
      }));
      dispatch({ type: "ui.session.select", sessionId, messages, replace: true });
      requestAnimationFrame(() => requestAnimationFrame(() => {
        void stickToBottomRef.current?.scrollToBottom({ animation: "instant", ignoreEscapes: true });
      }));
    } catch (error) {
      dispatch(errorFrame(error));
    } finally {
      if (loadingMessageWindowRef.current === loadingKey) loadingMessageWindowRef.current = "";
    }
  }, []);

  useEffect(() => {
    if (!chat.sessionId) return;
    const scroller = document.querySelector<HTMLElement>(".conversation-scroll");
    if (!scroller) return;
    const handleWheel = (event: WheelEvent) => {
      // use-stick-to-bottom 无法从 `hidden auto` shorthand 识别滚动容器，
      // 首次向上滚动时可能仍被吸底动画覆盖，主动解除锁定保证立即响应。
      if (event.deltaY < 0) stickToBottomRef.current?.stopScroll();
    };
    const handleScroll = () => {
      if (scroller.scrollTop <= 48) {
        void loadOlderMessages();
        return;
      }
      const distanceFromBottom = scroller.scrollHeight - scroller.clientHeight - scroller.scrollTop;
      if (distanceFromBottom <= 48) void loadNewerMessages();
    };
    scroller.addEventListener("wheel", handleWheel, { passive: true });
    scroller.addEventListener("scroll", handleScroll, { passive: true });
    return () => {
      scroller.removeEventListener("wheel", handleWheel);
      scroller.removeEventListener("scroll", handleScroll);
    };
  }, [chat.sessionId, loadNewerMessages, loadOlderMessages]);

  useEffect(() => {
    const handlePopState = () => {
      const sessionId = sessionFromPath(window.location.pathname);
      const previousSessionId = chatRef.current.sessionId;
      if (previousSessionId && previousSessionId !== sessionId) {
        rejectPendingApprovals(previousSessionId);
      }
      routeSessionRef.current = sessionId;
      setRouteSession(sessionId);
      setInput(textDrafts[routeKey(sessionId)] ?? "");
      setFiles(fileDrafts[routeKey(sessionId)] ?? []);
      if (sessionId) {
        void loadSession(sessionId);
        return;
      }
      dispatch({ type: "ui.session.select", sessionId: "", messages: [] });
    };
    window.addEventListener("popstate", handlePopState);
    return () => window.removeEventListener("popstate", handlePopState);
  }, [connection, fileDrafts, rejectPendingApprovals, textDrafts]);

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
    if (connection !== "connected") {
      dispatch({ type: "error", request_id: "", code: "offline", message: "连接尚未就绪，请重连后再发送" });
      return;
    }
    setSending(true);
    dispatch({ type: "ui.error.clear" });
    try {
      const uploaded = await Promise.all(files.map(uploadAttachment));
      const requestId = crypto.randomUUID();
      const sessionId = chat.sessionId;
      const optimistic: ChatMessage = {
        id: `user-${requestId}`,
        role: "user",
        content: cleanText,
        thinking: "",
        media: uploaded.map((item) => item.upload_path),
        tools: [],
        timestamp: new Date().toISOString(),
      };
      setRequestedTurnId("");
      dispatch({ type: "ui.user.append", message: optimistic });
      dispatch({ type: "ui.turn.submitted", sessionId, requestId });
      const sent = clientRef.current?.send({
        type: "message.send",
        request_id: requestId,
        ...(sessionId ? { session_id: sessionId } : {}),
        ...(!sessionId ? {
          workspace_id: newSessionWorkspaceId,
          sandbox_mode: newSessionMode,
          ...(newSessionMode === "danger-full-access" ? { risk_confirmed: true } : {}),
        } : {}),
        text: cleanText,
        media: uploaded.map((item) => item.upload_path),
      });
      if (!sent) {
        dispatch({
          type: "error",
          request_id: requestId,
          ...(sessionId ? { session_id: sessionId } : {}),
          code: "closed",
          message: "消息未发送，WebSocket 已断开",
        });
        return;
      }
      if (sessionId && messageWindowsRef.current[sessionId]?.hasTailWindow === false) {
        void returnToLatestMessages([optimistic]);
      }
      const submittedAt = new Date().toISOString();
      // 新会话的首轮可能长期运行或排队，发送成功后先放入目录，最终再由服务端标题覆盖。
      if (sessionId) setSessions((current) => current.some((session) => session.key === sessionId)
        ? current
        : [{
            key: sessionId,
            title: "新对话",
            created_at: submittedAt,
            updated_at: submittedAt,
            message_count: 0,
            first_message_content: "",
          }, ...current]);
      setInput("");
      setFiles([]);
      setTextDrafts((current) => ({ ...current, [routeKey(routeSession || sessionId)]: "", [routeKey("")]: "" }));
      setFileDrafts((current) => ({ ...current, [routeKey(routeSession || sessionId)]: [], [routeKey("")]: [] }));
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
      workspaces={workspaces}
      onCreate={createSession}
      onDelete={handleDeleteSession}
      onDeleteWorkspace={handleDeleteWorkspace}
      onRegisterWorkspace={handleRegisterWorkspace}
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

        <StickToBottom contextRef={stickToBottomRef} className="conversation" initial="instant" resize="instant" role="log">
          <StickToBottom.Content className="conversation-content" scrollClassName="conversation-scroll">
            {restoringSession ? <ConversationSkeleton /> : displayMessages.length === 0 ? <EmptyConversation /> : (
              <VirtualConversation
                key={chat.sessionId}
                groups={conversationTurnGroups}
                sessionId={chat.sessionId}
                requestedTurnId={requestedTurnId}
                onTurnPositioned={handleTurnPositioned}
              />
            )}
          </StickToBottom.Content>
          <TurnNavigator sessionId={chat.sessionId} turns={conversationTurns} onTurnRequest={handleTurnRequest} />
          <ConversationAutoScroll
            sessionId={chat.sessionId}
            messages={displayMessages}
            navigating={Boolean(requestedTurnId)}
          />
          <ConversationScrollButton
            hasTailWindow={Boolean(messageWindows[chat.sessionId]?.hasTailWindow ?? true)}
            onReturnToLatest={returnToLatestMessages}
          />
        </StickToBottom>

        {currentApproval ? (
          <ApprovalPanel
            approval={currentApproval}
            submitting={Boolean(approvalDecisionRequests[currentApproval.id])}
            onDecide={(decision) => decideApproval(currentApproval, decision)}
          />
        ) : (
          <Composer
            active={turnActive}
            turnStatus={currentTurn.status}
            queuePosition={currentTurn.queuePosition}
            connected={connection === "connected"}
            files={files}
            input={input}
            sessionId={chat.sessionId}
            contextUsage={currentContextUsage}
            sessionUsage={currentSessionUsage}
            workspaces={workspaces}
            workspaceId={currentWorkspaceId}
            workspaceValid={currentWorkspaceValid}
            sandboxMode={currentSandboxMode}
            sandboxUpdating={sandboxRequest?.sessionId === chat.sessionId}
            workspaceLocked={Boolean(chat.sessionId && (
              (currentSessionSummary?.message_count ?? 0) > 0 || chat.messages.length > 0
            ))}
            compacting={currentTurn.status === "compacting"
              || (compactionNotice.sessionId === chat.sessionId && compactionNotice.visible)}
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
            onWorkspaceChange={handleWorkspaceChange}
            onSandboxModeChange={handleSandboxModeChange}
          />
        )}
      </main>
    </div>
  );
}

function ConversationAutoScroll({ sessionId, messages, navigating }: {
  sessionId: string;
  messages: ChatMessage[];
  navigating: boolean;
}) {
  const { scrollToBottom } = useStickToBottomContext();
  const previousSessionRef = useRef("");
  const previousRuntimeUserIdRef = useRef("");

  useEffect(() => {
    let latestRuntimeUserId = "";
    for (let index = messages.length - 1; index >= 0; index -= 1) {
      const message = messages[index];
      if (message.role !== "user") continue;
      if (!message.id.startsWith("user-") && !(typeof message.seq === "number" && message.seq < 0)) continue;
      latestRuntimeUserId = message.id;
      break;
    }
    // 会话切换必须在目标消息已经进入 DOM 后再定位，且不能继承上一会话的
    // escaped lock；相同消息数量的两个会话也必须触发该边界。
    if (sessionId && messages.length > 0 && previousSessionRef.current !== sessionId) {
      previousSessionRef.current = sessionId;
      previousRuntimeUserIdRef.current = latestRuntimeUserId;
      // 恢复历史时先定位到末尾，但允许用户立即向上滚动；
      // ignoreEscapes=true 会在虚拟列表测量期间吞掉首个滚轮事件。
      void scrollToBottom({ animation: "instant" });
      return;
    }

    if (navigating) return;
    // turn.started 可能在本 effect 执行前追加 assistant 草稿，不能只检查列表最后一项。
    const hasNewUserMessage = Boolean(
      latestRuntimeUserId && latestRuntimeUserId !== previousRuntimeUserIdRef.current,
    );
    previousRuntimeUserIdRef.current = latestRuntimeUserId;
    if (hasNewUserMessage) {
      void scrollToBottom({ animation: "instant", ignoreEscapes: true });
      return;
    }
  }, [messages, navigating, sessionId, scrollToBottom]);

  return null;
}

function ConversationScrollButton({ hasTailWindow, onReturnToLatest }: {
  hasTailWindow: boolean;
  onReturnToLatest: () => Promise<void>;
}) {
  const { isAtBottom, scrollToBottom } = useStickToBottomContext();
  if (isAtBottom && hasTailWindow) return null;
  return (
    <button
      className="scroll-latest"
      aria-label="回到最新消息"
      title="回到最新消息"
      onClick={() => {
        if (!hasTailWindow) {
          void onReturnToLatest();
          return;
        }
        void scrollToBottom({ animation: "instant", ignoreEscapes: true });
      }}
    >
      <ArrowDown size={18} />
    </button>
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

function MessageView({ message, navigationTurnId, turnDurationMs }: {
  message: ChatMessage;
  navigationTurnId: string;
  turnDurationMs?: number;
}) {
  const isUser = message.role === "user";
  const parsed = useMemo(() => parseMemoryCitations(message.content), [message.content]);
  const [copied, setCopied] = useState(false);
  const copyTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const copyText = isUser || message.content !== "[用户已停止生成]" ? message.content : "";
  const assistantComplete = !isUser
    && !message.streaming
    && message.status !== "interrupted"
    && message.status !== "error"
    && Boolean(copyText.trim());
  const messageTime = formatMessageTime(message.timestamp);
  const duration = !isUser ? formatDuration(message.durationMs ?? turnDurationMs) : null;
  const metaClassName = isUser
    ? "message-meta user-message-meta"
    : `message-meta assistant-message-meta${assistantComplete ? " assistant-message-meta-complete" : ""}`;
  const handleCopy = useCallback(async () => {
    if (!copyText || !navigator.clipboard?.writeText) return;
    try {
      await navigator.clipboard.writeText(copyText);
      setCopied(true);
      if (copyTimerRef.current) clearTimeout(copyTimerRef.current);
      copyTimerRef.current = setTimeout(() => {
        copyTimerRef.current = null;
        setCopied(false);
      }, 1200);
    } catch {
      // 剪贴板权限失败不应改变消息内容或打断滚动。
    }
  }, [copyText]);
  useEffect(() => () => {
    if (copyTimerRef.current) clearTimeout(copyTimerRef.current);
  }, []);
  // Streamdown 在 isAnimating 期间会禁用复制和全屏按钮。Mermaid fence 一旦
  // 闭合就已经具备稳定源码，应立即放开查看大图，而不必等待 final 帧。
  const markdownAnimating = Boolean(message.streaming && !containsClosedMermaidFence(message.content));

  return (
    <article
      className={`message ${isUser ? "user-message" : "assistant-message"}`}
      data-turn-anchor={isUser ? navigationTurnId : undefined}
      tabIndex={0}
    >
      {!isUser ? <div className="message-label">BeanAgent</div> : null}
      <div className="message-body">
        {isUser ? <div className="message-label user-message-label">你</div> : null}
        {!isUser && message.source ? <MessageSourceBadge message={message} /> : null}
        {message.media.length ? <AttachmentGallery paths={message.media} /> : null}
        {message.thinking ? <Thinking content={message.thinking} streaming={Boolean(message.streaming)} status={message.thinkingStatus} /> : null}
        {message.tools.length ? <div className="tool-timeline">{message.tools.map((tool) => <ToolStep key={tool.callId} tool={tool} />)}</div> : null}
        {isUser ? <p className="user-text">{message.content}</p> : message.content && message.content !== "[用户已停止生成]" ? (
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
        {isUser && (messageTime || Boolean(copyText.trim())) ? (
          <div className={metaClassName}>
            {messageTime ? <time dateTime={message.timestamp}>{messageTime}</time> : null}
            {copyText.trim() ? (
              <button
                type="button"
                className="message-copy-button"
                aria-label={copied ? "已复制" : "复制问题"}
                title={copied ? "已复制" : "复制问题"}
                onClick={handleCopy}
              >
                {copied ? <Check size={14} aria-hidden="true" /> : <Copy size={14} aria-hidden="true" />}
              </button>
            ) : null}
          </div>
        ) : null}
        {!isUser && parsed.citations.length ? <MemoryCitationList citations={parsed.citations} /> : null}
        {!isUser && message.status === "interrupted" ? <span className="interrupted-label">已停止</span> : null}
        {!isUser && (messageTime || duration || assistantComplete) ? (
          <div className={metaClassName}>
            {assistantComplete ? (
              <button
                type="button"
                className="message-copy-button"
                aria-label={copied ? "已复制" : "复制回答"}
                title={copied ? "已复制" : "复制回答"}
                onClick={handleCopy}
              >
                {copied ? <Check size={14} aria-hidden="true" /> : <Copy size={14} aria-hidden="true" />}
              </button>
            ) : null}
            {messageTime ? <time dateTime={message.timestamp}>{messageTime}{duration ? ` · ${duration}` : ""}</time> : null}
          </div>
        ) : null}
      </div>
    </article>
  );
}

function formatMessageTime(timestamp?: string): string | null {
  if (!timestamp) return null;
  const date = new Date(timestamp);
  if (Number.isNaN(date.getTime())) return null;
  const month = date.getMonth() + 1;
  const day = date.getDate();
  const hours = String(date.getHours()).padStart(2, "0");
  const minutes = String(date.getMinutes()).padStart(2, "0");
  return `${month}月${day}日 ${hours}:${minutes}`;
}

function formatDuration(durationMs?: number): string | null {
  if (durationMs === undefined || !Number.isFinite(durationMs) || durationMs < 0) return null;
  if (durationMs < 1000) return "用时不到1秒";
  return `用时${Math.max(1, Math.round(durationMs / 1000))}秒`;
}

function deriveTurnDuration(messages: ChatMessage[]): number | undefined {
  const assistant = messages.find((message) => message.role === "assistant");
  return assistant?.durationMs;
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

function Thinking({ content, streaming, status }: { content: string; streaming: boolean; status?: "running" | "completed" | "interrupted" }) {
  const visibleStatus = status ?? (streaming ? "running" : "completed");
  return (
    <Collapsible.Root className={`thinking ${visibleStatus}`} defaultOpen={streaming}>
      <Collapsible.Trigger className="thinking-trigger">
        <Atom size={16} />
        <span>{visibleStatus === "interrupted" ? "已停止" : visibleStatus === "running" ? "正在思考…" : "思考完成"}</span>
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
  const icon = tool.status === "completed"
    ? <Check size={14} />
    : tool.status === "error"
      ? <AlertCircle size={14} />
      : tool.status === "interrupted"
        ? <CircleStop size={14} />
        : <Wrench size={14} />;
  return (
    <Collapsible.Root className={`tool-step ${tool.status}`}>
      <Collapsible.Trigger className="tool-trigger">
        <span className="tool-icon">{icon}</span><strong>{tool.name}</strong><span>{tool.status === "running" ? "执行中" : tool.status === "error" ? "失败" : tool.status === "interrupted" ? "已中断" : "完成"}</span><ChevronDown size={14} />
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
    return image ? <a className="image-attachment" href={mediaUrl(path)} target="_blank" rel="noreferrer" key={path}><img src={mediaUrl(path)} alt={fileName(path)} /></a> : (
      <a className="file-attachment" href={mediaUrl(path)} target="_blank" rel="noreferrer" key={path}><FileText size={17} /><span>{fileName(path)}</span></a>
    );
  })}</div>;
}

function Composer(props: {
  input: string; files: File[]; active: boolean; turnStatus: "idle" | "submitting" | "queued" | "running" | "compacting"; compacting: boolean; queuePosition: number | null; connected: boolean; sending: boolean; sessionId: string; contextUsage?: ContextUsage; sessionUsage?: SessionUsage;
  workspaces: Workspace[]; workspaceId: string | null; workspaceValid: boolean; sandboxMode: SandboxMode; sandboxUpdating: boolean; workspaceLocked: boolean;
  onInput: (value: string) => void; onFiles: (files: File[]) => void; onSend: () => void; onStop: () => void; onWorkspaceChange: (workspaceId: string | null) => void; onSandboxModeChange: (mode: SandboxMode, riskConfirmed?: boolean) => void;
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
  const sandboxControlsLocked = !props.connected || props.active || props.sending || props.sandboxUpdating;

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
        {props.compacting ? (
          <div className="compaction-notice" role="status" aria-live="polite">
            <RefreshCw size={15} aria-hidden="true" />
            <span>正在压缩上下文</span>
          </div>
        ) : null}
        <div className="composer-actions">
          <ContextUsageIndicator usage={props.contextUsage} compacting={props.compacting} />
          <WorkspaceSelector
            workspaces={props.workspaces}
            value={props.workspaceId}
            valid={props.workspaceValid}
            disabled={sandboxControlsLocked || props.workspaceLocked}
            onChange={props.onWorkspaceChange}
          />
          <PermissionSelector
            value={props.sandboxMode}
            hasWorkspace={props.workspaceId !== null}
            disabled={sandboxControlsLocked}
            onChange={props.onSandboxModeChange}
          />
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
          {props.active ? (
            <button className="send-button stop" aria-label="停止" onClick={handleStop}><CircleStop size={18} /><span>停止</span></button>
          ) : (
            <button className="send-button" aria-label="发送" onClick={handleSend} disabled={props.sending || (!props.input.trim() && props.files.length === 0)}><SendHorizontal size={18} /><span>发送</span></button>
          )}
        </div>
      </div>
      <SessionUsageStats usage={props.sessionUsage} />
      </footer>
    </>
  );
}

function SessionUsageStats({ usage }: { usage?: SessionUsage }) {
  if (!usage || (usage.totalInputTokens <= 0 && usage.totalOutputTokens <= 0)) return null;
  const hitRate = usage.totalInputTokens > 0 && usage.cacheHitRate !== null
    ? `${Math.round(usage.cacheHitRate * 100)}%`
    : "-";
  const detail = `累计输入 ${usage.totalInputTokens.toLocaleString()} token（未缓存 ${usage.totalUncachedInputTokens.toLocaleString()}，缓存读取 ${usage.totalCacheReadTokens.toLocaleString()}）\n累计输出 ${usage.totalOutputTokens.toLocaleString()} token`;
  return (
    <div className="session-usage-stats" title={detail} aria-label={detail}>
      <span>缓存命中 {hitRate}</span><span aria-hidden="true">|</span>
      <span>输入 {formatTokenCount(usage.totalInputTokens)} token</span><span aria-hidden="true">·</span>
      <span>输出 {formatTokenCount(usage.totalOutputTokens)} token</span>
    </div>
  );
}

export function ContextUsageIndicator({ usage, compacting }: { usage?: ContextUsage; compacting: boolean }) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return undefined;
    const onPointerDown = (event: PointerEvent) => {
      if (rootRef.current && !rootRef.current.contains(event.target as Node)) setOpen(false);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("pointerdown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);

  const contextWindow = usage?.contextWindow ?? 0;
  const usedTokens = usage?.usedTokens ?? 0;
  const known = Boolean(usage && contextWindow > 0 && usage.pressureTokens !== undefined);
  // DSH 只在真实 pressure 与模型容量同时存在时显示圆圈，避免估算值伪装成供应商用量。
  if (!known) return null;
  const percent = Math.min(100, Math.max(0, Math.round((usedTokens / contextWindow) * 100)));
  const level = percent >= 90 ? "danger" : percent >= 74 ? "warning" : "normal";
  const label = `上下文已用 ${percent}%`;
  const style = { "--usage-progress": `${percent}%` } as CSSProperties;
  const breakdown = usage?.breakdown;

  return (
    <div ref={rootRef} className={`context-usage-indicator ${level}${compacting ? " compacting" : ""}${open ? " open" : ""}`}>
      <button
        type="button"
        className="context-usage-button"
        style={style}
        aria-label={label}
        aria-expanded={open}
        aria-haspopup="dialog"
        onClick={() => setOpen((current) => !current)}
      >
        <span className="context-usage-ring" aria-hidden="true"><span /></span>
      </button>
      <span className="context-usage-tooltip" role="tooltip">{label}</span>
      {open ? (
        <div className="context-usage-popover" role="dialog" aria-label="上下文占用详情">
          <div className="context-usage-popover-header">
            <strong>上下文占用</strong>
            <span>~{formatTokenCount(usedTokens)} / {formatTokenCount(contextWindow)}</span>
          </div>
          <div className="context-usage-meter" aria-label={label}>
            <span style={{ width: `${percent}%` }} />
          </div>
          {compacting ? <p className="context-usage-state">正在压缩上下文，当前数值保持不变</p> : null}
          {breakdown ? (
            <dl className="context-usage-breakdown">
              <div><dt>系统提示词</dt><dd>{formatTokenCount(breakdown.system_prompt_tokens)}</dd></div>
              <div><dt>工具</dt><dd>{formatTokenCount(breakdown.tools_tokens)}</dd></div>
              <div><dt>对话消息</dt><dd>{formatTokenCount(breakdown.conversation_tokens)}</dd></div>
            </dl>
          ) : <p className="context-usage-empty">等待本轮上下文估算</p>}
          <small className="context-usage-source">
            {usage?.estimateSource === "provider_usage" ? "Provider usage" : usage?.estimateSource === "provider_projected" ? "投影值" : "估算值"}
            {" · "}{usage?.contextWindowSource || "unknown"}
          </small>
        </div>
      ) : null}
    </div>
  );
}

function formatTokenCount(value: number): string {
  const count = Math.max(0, Math.round(value));
  if (count >= 1_000_000) return `${(count / 1_000_000).toFixed(count >= 10_000_000 ? 0 : 1).replace(/\.0$/, "")}M`;
  if (count >= 1_000) return `${(count / 1_000).toFixed(count >= 100_000 ? 0 : 1).replace(/\.0$/, "")}K`;
  return String(count);
}

function VirtualConversation({ groups, sessionId, requestedTurnId, onTurnPositioned }: {
  groups: ReturnType<typeof groupMessagesIntoNavigationTurns>;
  sessionId: string;
  requestedTurnId: string;
  onTurnPositioned: (turnId: string) => void;
}) {
  const hostRef = useRef<HTMLDivElement>(null);
  const [scrollElement, setScrollElement] = useState<HTMLElement | null>(null);
  const virtualizer = useVirtualizer({
    count: groups.length,
    anchorTo: "end",
    getScrollElement: () => scrollElement,
    estimateSize: () => 220,
    getItemKey: (index) => `${groups[index].navigationTurnId}:${groups[index].messages[0].id}`,
    measureElement: (element) => element.getBoundingClientRect().height,
    initialRect: { width: 880, height: 800 },
    observeElementRect: (instance, callback) => {
      const element = instance.scrollElement;
      if (!element) return undefined;
      const update = () => callback({
        width: element.clientWidth || 880,
        height: element.clientHeight || 800,
      });
      update();
      if (typeof ResizeObserver === "undefined") return undefined;
      const observer = new ResizeObserver(update);
      observer.observe(element);
      return () => observer.disconnect();
    },
    overscan: 4,
  });

  useLayoutEffect(() => {
    setScrollElement(hostRef.current?.closest<HTMLElement>(".conversation-scroll") ?? null);
  }, [sessionId]);

  useLayoutEffect(() => {
    if (!requestedTurnId || !scrollElement) return;
    const index = groups.findIndex((group) => group.navigationTurnId === requestedTurnId);
    if (index < 0) return;
    virtualizer.scrollToIndex(index, { align: "start", behavior: "auto" });
    onTurnPositioned(requestedTurnId);
  }, [groups, onTurnPositioned, requestedTurnId, scrollElement, virtualizer]);

  return (
    <div
      ref={hostRef}
      className="virtual-conversation"
      style={{ height: `${virtualizer.getTotalSize()}px` }}
    >
      {virtualizer.getVirtualItems().map((item) => {
        const group = groups[item.index];
        const turnDurationMs = deriveTurnDuration(group.messages);
        return (
          <section
            key={item.key}
            ref={virtualizer.measureElement}
            className="turn-section virtual-turn-section"
            data-index={item.index}
            data-turn-region={group.navigationTurnId || undefined}
            style={{ transform: `translateY(${item.start}px)` }}
          >
            {group.messages.map((message) => (
              <MessageView
                key={message.id}
                message={message}
                navigationTurnId={group.navigationTurnId}
                turnDurationMs={turnDurationMs}
              />
            ))}
          </section>
        );
      })}
    </div>
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

function upsertMessage(messages: ChatMessage[], incoming: ChatMessage): ChatMessage[] {
  const index = messages.findIndex((message) => message.id === incoming.id);
  if (index < 0) return [...messages, incoming];
  const next = [...messages];
  next[index] = { ...next[index], ...incoming };
  return next;
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

function nextSeqFromRows(rows: MessageRow[]): number | null {
  const seqs = rows.map((row) => row.seq).filter((seq): seq is number => typeof seq === "number" && seq >= 0);
  return seqs.length ? Math.max(...seqs) + 1 : null;
}

function shortSession(sessionId: string): string { return sessionId ? `会话 ${sessionId.slice(-8)}` : "正在创建会话"; }
function fileName(path: string): string { return path.replaceAll("\\", "/").split("/").pop() || "附件"; }
function fileSuffix(name: string): string { const index = name.lastIndexOf("."); return index >= 0 ? name.slice(index).toLowerCase() : ""; }
function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Number((bytes / 1024).toFixed(1))} KB`;
  return `${Number((bytes / 1024 / 1024).toFixed(1))} MB`;
}
