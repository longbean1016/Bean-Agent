import * as Dialog from "@radix-ui/react-dialog";
import {
  AlertCircle,
  Bell,
  Folder,
  FolderOpen,
  FolderPlus,
  MessageSquarePlus,
  MoreHorizontal,
  Pencil,
  Pin,
  PinOff,
  Settings,
  Trash2,
  Unlink,
  X,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";

import {
  deleteReminder,
  fetchProactiveSettings,
  fetchReminders,
  pickWorkspaceDirectory,
  saveProactiveSettings,
} from "./api";
import type {
  ProactiveSettings,
  ScheduledReminder,
  SessionSummary,
  Workspace,
} from "./types";

export function SessionSidebar(props: {
  sessions: SessionSummary[];
  workspaces: Workspace[];
  activeSessionId: string;
  onCreate: (workspaceId?: string | null) => void;
  onDelete: (id: string) => Promise<void>;
  onDeleteWorkspace: (id: string) => Promise<void>;
  onOpenWorkspace: (id: string) => Promise<void>;
  onRegisterWorkspace: (path: string, title: string) => Promise<Workspace>;
  onRename: (id: string, title: string) => Promise<void>;
  onSetSessionPinned: (id: string, pinned: boolean) => Promise<void>;
  onUpdateWorkspace: (id: string, patch: { title?: string; pinned?: boolean }) => Promise<void>;
  onSelect: (id: string) => void;
  onSettings: () => void;
}) {
  const [menuSessionId, setMenuSessionId] = useState("");
  const [menuWorkspaceId, setMenuWorkspaceId] = useState("");
  const [editingSessionId, setEditingSessionId] = useState("");
  const [titleDraft, setTitleDraft] = useState("");
  const [deleteTarget, setDeleteTarget] = useState<SessionSummary | null>(null);
  const [deleteWorkspaceTarget, setDeleteWorkspaceTarget] = useState<Workspace | null>(null);
  const [workspaceDialogOpen, setWorkspaceDialogOpen] = useState(false);
  const [workspacePath, setWorkspacePath] = useState("");
  const [workspaceTitle, setWorkspaceTitle] = useState("");
  const [workspaceError, setWorkspaceError] = useState("");
  const [pickingWorkspace, setPickingWorkspace] = useState(false);
  const [renameWorkspaceTarget, setRenameWorkspaceTarget] = useState<Workspace | null>(null);
  const [workspaceTitleDraft, setWorkspaceTitleDraft] = useState("");
  const [updatingWorkspace, setUpdatingWorkspace] = useState(false);
  const [proactiveTarget, setProactiveTarget] = useState<SessionSummary | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [deletingWorkspace, setDeletingWorkspace] = useState(false);
  const [registeringWorkspace, setRegisteringWorkspace] = useState(false);
  const [scrollbarVisible, setScrollbarVisible] = useState(false);
  const scrollbarHideTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const sessionListRef = useRef<HTMLElement>(null);

  useEffect(() => {
    // 新会话发送消息后出现在侧栏时，自动滚动定位到该会话。
    const activeRow = sessionListRef.current?.querySelector(".session-row.active");
    if (activeRow && typeof activeRow.scrollIntoView === "function") {
      try {
        activeRow.scrollIntoView({ behavior: "smooth", block: "nearest" });
      } catch {
        // jsdom 环境可能不支持 scrollIntoView。
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
    // 移出后保留短暂视觉提示，避免滚动位置突然失去参照。
    scrollbarHideTimerRef.current = setTimeout(() => {
      setScrollbarVisible(false);
      scrollbarHideTimerRef.current = null;
    }, 3_000);
  };

  useEffect(() => {
    if (!menuSessionId && !menuWorkspaceId) return;
    const closeMenuOutside = (event: PointerEvent) => {
      const sessionOwner = event.target instanceof Element
        ? event.target.closest<HTMLElement>("[data-session-menu-owner]")
        : null;
      const workspaceOwner = event.target instanceof Element
        ? event.target.closest<HTMLElement>("[data-workspace-menu-owner]")
        : null;
      if (sessionOwner?.dataset.sessionMenuOwner !== menuSessionId) setMenuSessionId("");
      if (workspaceOwner?.dataset.workspaceMenuOwner !== menuWorkspaceId) setMenuWorkspaceId("");
    };
    // 捕获阶段先收起菜单，再执行目标控件自己的动作。
    document.addEventListener("pointerdown", closeMenuOutside, true);
    return () => document.removeEventListener("pointerdown", closeMenuOutside, true);
  }, [menuSessionId, menuWorkspaceId]);

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

  const renderSessionRows = (sessions: SessionSummary[]) => sortSessions(sessions).map((session) => {
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
            <span>{session.pinned_at ? <Pin className="session-row-pin" size={13} /> : null}{title}</span>
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
            {session.workspace_id ? (
              <button role="menuitem" onClick={() => {
                setMenuSessionId("");
                void props.onSetSessionPinned(session.key, !session.pinned_at).catch(() => undefined);
              }}>{session.pinned_at ? <PinOff size={15} /> : <Pin size={15} />}{session.pinned_at ? "取消置顶" : "置顶会话"}</button>
            ) : null}
            <button role="menuitem" onClick={() => { setMenuSessionId(""); setProactiveTarget(session); }}><Bell size={15} />主动设置</button>
            <button role="menuitem" onClick={() => beginRename(session)}><Pencil size={15} />重命名</button>
            <button className="danger" role="menuitem" onClick={() => { setMenuSessionId(""); setDeleteTarget(session); }}><Trash2 size={15} />删除</button>
          </div>
        ) : null}
      </div>
    );
  });

  const renderSessions = (sessions: SessionSummary[]) => sessions.length
    ? renderSessionRows(sessions)
    : <p className="workspace-session-empty">尚无会话</p>;

  const chooseWorkspace = async () => {
    if (pickingWorkspace || registeringWorkspace) return;
    setPickingWorkspace(true);
    setWorkspaceError("");
    try {
      const selected = await pickWorkspaceDirectory();
      if (selected !== null) setWorkspacePath(selected);
    } catch (error) {
      setWorkspaceError(error instanceof Error ? error.message : "无法选择工作目录");
    } finally {
      setPickingWorkspace(false);
    }
  };

  const submitWorkspace = async () => {
    const path = workspacePath.trim();
    if (!path || registeringWorkspace) return;
    setRegisteringWorkspace(true);
    setWorkspaceError("");
    try {
      await props.onRegisterWorkspace(path, workspaceTitle.trim());
      setWorkspaceDialogOpen(false);
      setWorkspacePath("");
      setWorkspaceTitle("");
    } catch (error) {
      setWorkspaceError(error instanceof Error ? error.message : "无法添加工作目录");
    } finally {
      setRegisteringWorkspace(false);
    }
  };

  const unassignedSessions = props.sessions.filter((session) => !session.workspace_id);
  const pinnedWorkspaces = props.workspaces
    .filter((workspace) => workspace.pinned_at)
    .sort(comparePinned);
  const projectWorkspaces = props.workspaces
    .filter((workspace) => !workspace.pinned_at)
    .sort((left, right) => compareDateDesc(left.created_at, right.created_at, left.id, right.id));
  const registeredWorkspaceIds = new Set(props.workspaces.map((workspace) => workspace.id));
  const unavailableByWorkspace = new Map<string, {
    id: string;
    title: string;
    path: string;
    sessions: SessionSummary[];
  }>();
  for (const session of props.sessions) {
    const workspaceId = session.workspace_id;
    if (!workspaceId || registeredWorkspaceIds.has(workspaceId)) continue;
    const existing = unavailableByWorkspace.get(workspaceId);
    if (existing) {
      existing.sessions.push(session);
      continue;
    }
    unavailableByWorkspace.set(workspaceId, {
      id: workspaceId,
      title: session.workspace_title || (
        session.workspace_path ? workspaceName(session.workspace_path) : "不可用工作目录"
      ),
      path: session.workspace_path || "",
      sessions: [session],
    });
  }
  const unavailableWorkspaceGroups = [...unavailableByWorkspace.values()];

  const renderWorkspace = (workspace: Workspace) => (
    <section className="workspace-group" key={workspace.id} data-workspace-id={workspace.id}>
      <header className="workspace-group-header" data-workspace-menu-owner={workspace.id}>
        <span className={`workspace-group-icon${workspace.valid ? "" : " invalid"}`}><Folder size={15} /></span>
        <span className="workspace-group-copy" title={workspace.canonical_path}>
          <strong>{workspace.title || workspaceName(workspace.canonical_path)}</strong>
          <small>{workspace.valid ? workspace.canonical_path : "目录不可用"}</small>
        </span>
        <button className="workspace-header-action" aria-label={`在“${workspace.title || workspaceName(workspace.canonical_path)}”中新建会话`} title="新建会话" disabled={!workspace.valid} onClick={() => props.onCreate(workspace.id)}><MessageSquarePlus size={15} /></button>
        <button className="workspace-header-action" aria-label={`打开工作目录“${workspace.title || workspaceName(workspace.canonical_path)}”的菜单`} aria-expanded={menuWorkspaceId === workspace.id} title="工作目录菜单" onClick={() => setMenuWorkspaceId((current) => current === workspace.id ? "" : workspace.id)}><MoreHorizontal size={16} /></button>
        {menuWorkspaceId === workspace.id ? (
          <div className="workspace-menu" role="menu">
            <button role="menuitem" onClick={() => { setMenuWorkspaceId(""); void props.onUpdateWorkspace(workspace.id, { pinned: !workspace.pinned_at }).catch(() => undefined); }}>{workspace.pinned_at ? <PinOff size={15} /> : <Pin size={15} />}{workspace.pinned_at ? "取消置顶" : "置顶项目"}</button>
            <button role="menuitem" onClick={() => { setMenuWorkspaceId(""); setWorkspaceTitleDraft(workspace.title); setRenameWorkspaceTarget(workspace); }}><Pencil size={15} />修改名称</button>
            <button role="menuitem" disabled={!workspace.valid} onClick={() => { setMenuWorkspaceId(""); void props.onOpenWorkspace(workspace.id).catch(() => undefined); }}><FolderOpen size={15} />在资源管理器中打开</button>
            <button className="danger" role="menuitem" onClick={() => { setMenuWorkspaceId(""); setDeleteWorkspaceTarget(workspace); }}><Unlink size={15} />移除工作目录</button>
          </div>
        ) : null}
      </header>
      <div className="workspace-sessions">{renderSessions(props.sessions.filter((session) => session.workspace_id === workspace.id))}</div>
    </section>
  );

  return (
    <div className="session-panel">
      <div className="brand-lockup">
        <span className="brand-mark">B</span>
        <strong>BeanAgent</strong>
      </div>
      <div className="sidebar-primary-actions">
        <button className="new-chat-button" onClick={() => props.onCreate(null)}><MessageSquarePlus size={17} />新建会话</button>
        <button className="add-workspace-button" onClick={() => {
          setWorkspaceError("");
          setWorkspacePath("");
          setWorkspaceTitle("");
          setWorkspaceDialogOpen(true);
        }}><FolderPlus size={17} />添加工作目录</button>
      </div>
      <nav
        ref={sessionListRef}
        className={`session-list ${scrollbarVisible ? "scrollbar-visible" : ""}`}
        aria-label="会话列表"
        onPointerEnter={showSessionScrollbar}
        onPointerLeave={scheduleSessionScrollbarHide}
      >
        {pinnedWorkspaces.length ? <h2 className="sidebar-section-label">置顶</h2> : null}
        {pinnedWorkspaces.map(renderWorkspace)}
        {projectWorkspaces.length || unavailableWorkspaceGroups.length ? <h2 className="sidebar-section-label">项目</h2> : null}
        {projectWorkspaces.map(renderWorkspace)}
        {unavailableWorkspaceGroups.map((workspace) => (
          <section className="workspace-group" key={`unavailable:${workspace.id}`} data-workspace-id={workspace.id}>
            <header className="workspace-group-header unavailable-header">
              <span className="workspace-group-icon invalid"><Folder size={15} /></span>
              <span className="workspace-group-copy" title={workspace.path || undefined}>
                <strong>{workspace.title}</strong>
                <small>{workspace.path || "原工作目录未注册或加载失败"}</small>
              </span>
            </header>
            <div className="workspace-sessions">{renderSessions(workspace.sessions)}</div>
          </section>
        ))}
        <div className="sidebar-section-heading recent-heading">
          <h2 className="sidebar-section-label recent-label">最近</h2>
          <button className="workspace-header-action" aria-label="在最近中新建会话" title="新建会话" onClick={() => props.onCreate(null)}><MessageSquarePlus size={15} /></button>
        </div>
        <div className="recent-sessions" data-workspace-id="none">{renderSessions(unassignedSessions)}</div>
      </nav>
      <button className="sidebar-settings-button" onClick={props.onSettings} title="模型与连接设置"><Settings size={17} />设置</button>
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
      <Dialog.Root open={workspaceDialogOpen} onOpenChange={(open) => {
        if (registeringWorkspace || pickingWorkspace) return;
        setWorkspaceDialogOpen(open);
        if (!open) setWorkspaceError("");
      }}>
        <Dialog.Portal>
          <Dialog.Overlay className="dialog-overlay" />
          <Dialog.Content className="workspace-dialog workspace-create-dialog">
            <div className="workspace-dialog-header">
              <div><Dialog.Title>添加工作目录</Dialog.Title><Dialog.Description className="sr-only">注册本机已有目录，用于限定会话的可写范围。</Dialog.Description></div>
              <Dialog.Close asChild><button className="icon-button" aria-label="关闭" disabled={registeringWorkspace || pickingWorkspace}><X size={17} /></button></Dialog.Close>
            </div>
            <form onSubmit={(event) => { event.preventDefault(); void submitWorkspace(); }}>
              <label className="workspace-name-field">
                <span className="sr-only">显示名称（可选）</span>
                <Folder size={20} aria-hidden="true" />
                <input
                  value={workspaceTitle}
                  maxLength={80}
                  onChange={(event) => setWorkspaceTitle(event.target.value)}
                  placeholder="项目名称（可选）"
                />
              </label>
              <div className="workspace-picker-field">
                <span className="workspace-field-label">源文件夹</span>
                <button
                  type="button"
                  className={`workspace-picker-card${workspacePath ? " selected" : ""}`}
                  disabled={pickingWorkspace || registeringWorkspace}
                  onClick={() => void chooseWorkspace()}
                  title={workspacePath || "选择工作目录"}
                  aria-label={workspacePath ? `重新选择工作目录，当前为 ${workspacePath}` : "选择 Bean 可读取和编辑的文件夹"}
                >
                  <span className="workspace-picker-icon">
                    {workspacePath ? <FolderOpen size={26} /> : <FolderPlus size={26} />}
                  </span>
                  <span className="workspace-picker-copy">
                    <strong>{workspacePath
                      ? workspaceName(workspacePath)
                      : "添加 Bean 可读取和编辑的文件夹"}</strong>
                    {workspacePath ? <small>{workspacePath}</small> : null}
                  </span>
                </button>
              </div>
              {workspaceError ? <p className="workspace-form-error" role="alert">{workspaceError}</p> : null}
              <footer className="workspace-dialog-actions">
                <Dialog.Close asChild><button type="button" className="secondary-action" disabled={registeringWorkspace || pickingWorkspace}>取消</button></Dialog.Close>
                <button type="submit" className="primary-action" disabled={registeringWorkspace || pickingWorkspace || !workspacePath.trim()}>{registeringWorkspace ? "添加中…" : "添加"}</button>
              </footer>
            </form>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>
      <Dialog.Root open={renameWorkspaceTarget !== null} onOpenChange={(open) => {
        if (!open && !updatingWorkspace) setRenameWorkspaceTarget(null);
      }}>
        <Dialog.Portal>
          <Dialog.Overlay className="dialog-overlay" />
          <Dialog.Content className="workspace-dialog rename-workspace-dialog">
            <div className="workspace-dialog-header">
              <div><Dialog.Title>修改项目名称</Dialog.Title><Dialog.Description>只修改 BeanAgent 中显示的名称，不会重命名磁盘文件夹。</Dialog.Description></div>
              <Dialog.Close asChild><button className="icon-button" aria-label="关闭" disabled={updatingWorkspace}><X size={17} /></button></Dialog.Close>
            </div>
            <form onSubmit={(event) => {
              event.preventDefault();
              const title = workspaceTitleDraft.trim();
              if (!renameWorkspaceTarget || !title || updatingWorkspace) return;
              setUpdatingWorkspace(true);
              void props.onUpdateWorkspace(renameWorkspaceTarget.id, { title })
                .then(() => setRenameWorkspaceTarget(null))
                .catch(() => undefined)
                .finally(() => setUpdatingWorkspace(false));
            }}>
              <label>项目名称<input autoFocus value={workspaceTitleDraft} maxLength={80} onChange={(event) => setWorkspaceTitleDraft(event.target.value)} /></label>
              <footer className="workspace-dialog-actions">
                <Dialog.Close asChild><button type="button" className="secondary-action" disabled={updatingWorkspace}>取消</button></Dialog.Close>
                <button type="submit" className="primary-action" disabled={updatingWorkspace || !workspaceTitleDraft.trim()}>{updatingWorkspace ? "保存中…" : "保存"}</button>
              </footer>
            </form>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>
      <Dialog.Root open={deleteWorkspaceTarget !== null} onOpenChange={(open) => { if (!open && !deletingWorkspace) setDeleteWorkspaceTarget(null); }}>
        <Dialog.Portal>
          <Dialog.Overlay className="dialog-overlay" />
          <Dialog.Content className="delete-session-dialog">
            <Dialog.Title>移除“{deleteWorkspaceTarget ? (deleteWorkspaceTarget.title || workspaceName(deleteWorkspaceTarget.canonical_path)) : ""}”？</Dialog.Title>
            <Dialog.Description>
              只会移除 BeanAgent 中的注册和会话关联。<br />
              磁盘上的目录和文件不会被删除，原有会话将归入无工作目录。
            </Dialog.Description>
            <div className="delete-dialog-actions">
              <Dialog.Close asChild><button disabled={deletingWorkspace}>取消</button></Dialog.Close>
              <button
                className="confirm-delete"
                disabled={deletingWorkspace}
                onClick={() => {
                  if (!deleteWorkspaceTarget) return;
                  setDeletingWorkspace(true);
                  void props.onDeleteWorkspace(deleteWorkspaceTarget.id)
                    .then(() => setDeleteWorkspaceTarget(null))
                    .catch(() => undefined)
                    .finally(() => setDeletingWorkspace(false));
                }}
              >确认移除</button>
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

function workspaceName(path: string): string {
  const normalized = path.replace(/[\\/]+$/, "");
  return normalized.split(/[\\/]/).pop() || path || "工作目录";
}

function compareDateDesc(left: string, right: string, leftId: string, rightId: string): number {
  const byDate = Date.parse(right) - Date.parse(left);
  return Number.isNaN(byDate) || byDate === 0 ? rightId.localeCompare(leftId) : byDate;
}

function comparePinned<T extends { pinned_at?: string | null; id?: string; key?: string }>(left: T, right: T): number {
  const leftTime = left.pinned_at ? Date.parse(left.pinned_at) : Number.POSITIVE_INFINITY;
  const rightTime = right.pinned_at ? Date.parse(right.pinned_at) : Number.POSITIVE_INFINITY;
  const byDate = leftTime - rightTime;
  return Number.isNaN(byDate) || byDate === 0
    ? (left.id ?? left.key ?? "").localeCompare(right.id ?? right.key ?? "")
    : byDate;
}

function sortSessions(sessions: SessionSummary[]): SessionSummary[] {
  return [...sessions].sort((left, right) => {
    if (left.pinned_at && right.pinned_at) return comparePinned(left, right);
    if (left.pinned_at) return -1;
    if (right.pinned_at) return 1;
    return compareDateDesc(
      left.last_activity_at || left.created_at,
      right.last_activity_at || right.created_at,
      left.key,
      right.key,
    );
  });
}
