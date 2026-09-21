import * as Dialog from "@radix-ui/react-dialog";
import { Check, ChevronDown, Folder, Shield, ShieldAlert } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";

import type { ApprovalRequest, SandboxMode, Workspace } from "./types";

type ComposerMenuOption = {
  id: string;
  label: string;
  description?: string;
  disabled?: boolean;
  icon: ReactNode;
};

function ComposerMenu(props: {
  label: string;
  value: string;
  options: ComposerMenuOption[];
  disabled: boolean;
  onChange: (id: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const selected = props.options.find((option) => option.id === props.value) ?? props.options[0];

  useEffect(() => {
    if (props.disabled) setOpen(false);
  }, [props.disabled]);
  useEffect(() => {
    if (!open) return undefined;
    const close = (event: PointerEvent) => {
      if (rootRef.current && !rootRef.current.contains(event.target as Node)) setOpen(false);
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("pointerdown", close);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("pointerdown", close);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [open]);

  return (
    <div className="composer-select" ref={rootRef}>
      <button
        type="button"
        className="composer-select-trigger"
        aria-label={`${props.label}：${selected.label}`}
        aria-haspopup="menu"
        aria-expanded={open}
        disabled={props.disabled}
        onClick={() => setOpen((current) => !current)}
      >
        <span className="composer-select-icon" aria-hidden="true">{selected.icon}</span>
        <span className="composer-select-label">{selected.label}</span>
        <ChevronDown size={14} className={open ? "open" : ""} aria-hidden="true" />
      </button>
      {open ? (
        <div className="composer-select-menu" role="menu" aria-label={props.label}>
          {props.options.map((option) => (
            <button
              key={option.id}
              type="button"
              role="menuitemradio"
              aria-checked={option.id === props.value}
              disabled={option.disabled}
              title={option.disabled ? option.description : undefined}
              onClick={() => {
                setOpen(false);
                if (option.id !== props.value) props.onChange(option.id);
              }}
            >
              <span className="composer-option-icon" aria-hidden="true">{option.icon}</span>
              <span className="composer-option-copy">
                <strong>{option.label}</strong>
                {option.description ? <small>{option.description}</small> : null}
              </span>
              {option.id === props.value ? <Check size={15} aria-hidden="true" /> : null}
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}

export function WorkspaceSelector(props: {
  workspaces: Workspace[];
  value: string | null;
  valid: boolean;
  disabled: boolean;
  onChange: (workspaceId: string | null) => void;
}) {
  const options: ComposerMenuOption[] = [{
    id: "none",
    label: "无工作目录",
    description: "使用会话私有临时目录",
    icon: <Folder size={15} />,
  }, ...props.workspaces.map((workspace) => ({
    id: workspace.id,
    label: workspace.title || workspaceName(workspace.canonical_path),
    description: workspace.valid ? workspace.canonical_path : "目录不可用",
    disabled: !workspace.valid,
    icon: <Folder size={15} />,
  }))];
  if (props.value && !options.some((option) => option.id === props.value)) {
    options.push({
      id: props.value,
      label: "目录不可用",
      description: "原工作目录未注册或已移除",
      disabled: true,
      icon: <Folder size={15} />,
    });
  }
  if (props.value && !props.valid) {
    const current = options.find((option) => option.id === props.value);
    if (current) current.disabled = true;
  }
  return (
    <ComposerMenu
      label="工作目录"
      value={props.value ?? "none"}
      options={options}
      disabled={props.disabled}
      onChange={(id) => props.onChange(id === "none" ? null : id)}
    />
  );
}

export function PermissionSelector(props: {
  value: SandboxMode;
  hasWorkspace: boolean;
  disabled: boolean;
  onChange: (mode: SandboxMode, riskConfirmed?: boolean) => void;
}) {
  const [confirmationOpen, setConfirmationOpen] = useState(false);
  const [acknowledged, setAcknowledged] = useState(false);
  const options: ComposerMenuOption[] = [
    { id: "read-only", label: "只读", description: "禁止文件写入，可申请单次授权", icon: <Shield size={15} /> },
    { id: "workspace-write", label: "工作区可写", description: props.hasWorkspace ? "仅允许写入当前工作目录" : "请先选择工作目录", disabled: !props.hasWorkspace, icon: <Shield size={15} /> },
    { id: "danger-full-access", label: "完全访问", description: "使用当前 Windows 用户原有权限", icon: <ShieldAlert size={15} /> },
  ];
  const closeConfirmation = () => {
    setAcknowledged(false);
    setConfirmationOpen(false);
  };
  return (
    <>
      <ComposerMenu
        label="权限"
        value={props.value}
        options={options}
        disabled={props.disabled}
        onChange={(id) => {
          const mode = id as SandboxMode;
          if (mode === "danger-full-access") {
            setAcknowledged(false);
            setConfirmationOpen(true);
          } else {
            props.onChange(mode);
          }
        }}
      />
      <Dialog.Root open={confirmationOpen} onOpenChange={(open) => { if (!open) closeConfirmation(); }}>
        <Dialog.Portal>
          <Dialog.Overlay className="dialog-overlay" />
          <Dialog.Content className="risk-dialog">
            <span className="risk-dialog-icon" aria-hidden="true"><ShieldAlert size={22} /></span>
            <Dialog.Title>启用完全访问？</Dialog.Title>
            <Dialog.Description>命令和文件操作将绕过 BeanAgent 的本地写入限制，并使用当前 Windows 用户原有权限。</Dialog.Description>
            <label className="risk-acknowledgement">
              <input type="checkbox" checked={acknowledged} onChange={(event) => setAcknowledged(event.target.checked)} />
              <span>我了解该会话可能修改或删除工作目录外的文件</span>
            </label>
            <div className="risk-dialog-actions">
              <button type="button" className="secondary-action" onClick={closeConfirmation}>取消</button>
              <button
                type="button"
                className="danger-action"
                disabled={!acknowledged || props.disabled}
                onClick={() => {
                  if (!acknowledged || props.disabled) return;
                  closeConfirmation();
                  props.onChange("danger-full-access", true);
                }}
              >启用完全访问</button>
            </div>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>
    </>
  );
}

export function ApprovalCard(props: {
  approval: ApprovalRequest;
  submitting: boolean;
  onDecide: (decision: "allowed-once" | "rejected") => void;
  inline?: boolean;
  queuePosition?: number;
  queueTotal?: number;
}) {
  const entries = safeApprovalEntries(props.approval);
  const summary = props.approval.summary?.trim() || "";
  const reason = props.approval.reason?.trim() || "";
  const queueLabel = props.queuePosition && props.queueTotal && props.queueTotal > 1
    ? `队列 ${props.queuePosition}/${props.queueTotal}`
    : "";
  return (
    <section className={`approval-panel${props.inline ? " approval-inline" : ""}`} aria-label="待处理权限审批" aria-live="polite">
        <div className="approval-strip"><span aria-hidden="true" />等待授权{queueLabel ? <small>{queueLabel}</small> : null}</div>
        <div className="approval-body" tabIndex={0} role="group" aria-label="越权操作详情">
          <strong>{summary || reason || `${props.approval.tool_name} 请求临时提高权限`}</strong>
          {summary && reason && summary !== reason ? <span className="approval-reason">原因：{reason}</span> : null}
          <span className="approval-operation">{props.approval.operation}</span>
          {entries.length ? (
            <dl className="approval-arguments">
              {entries.map(([key, value]) => (
                <div key={key}><dt>{approvalArgumentLabel(key)}</dt><dd>{displayApprovalArgument(value)}</dd></div>
              ))}
            </dl>
          ) : null}
        </div>
        <div className="approval-actions">
          <button className="secondary-action approval-reject" disabled={props.submitting} onClick={() => props.onDecide("rejected")}>拒绝</button>
          <button className="primary-action" disabled={props.submitting} onClick={() => props.onDecide("allowed-once")}>{props.submitting ? "提交中…" : "仅允许本次"}</button>
        </div>
      </section>
  );
}

export function ApprovalPanel(props: {
  approval: ApprovalRequest;
  submitting: boolean;
  onDecide: (decision: "allowed-once" | "rejected") => void;
  queuePosition?: number;
  queueTotal?: number;
}) {
  return (
    <footer className="composer-wrap approval-wrap">
      <ApprovalCard {...props} />
    </footer>
  );
}

function workspaceName(path: string): string {
  const normalized = path.replace(/[\\/]+$/, "");
  return normalized.split(/[\\/]/).pop() || path || "工作目录";
}

function approvalArgumentLabel(key: string): string {
  const labels: Record<string, string> = {
    command: "完整命令",
    path: "目标路径",
    source: "源路径",
    destination: "目标路径",
    cwd: "执行目录",
    description: "命令说明",
    timeout: "超时秒数",
    content_length: "写入字符数",
    old_text_length: "原文本字符数",
    new_text_length: "新文本字符数",
    replace_all: "替换全部匹配",
  };
  return labels[key] ?? key;
}

function displayApprovalArgument(value: unknown): string {
  if (typeof value === "string") return value;
  if (typeof value === "boolean") return value ? "是" : "否";
  if (value === null || value === undefined) return "-";
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

/** 审批卡只展示后端允许的摘要字段，避免把正文、token 或内部参数原样送进 UI。 */
function safeApprovalEntries(approval: ApprovalRequest): Array<[string, unknown]> {
  const allowed = new Set([
    "path", "source", "destination", "cwd", "command", "pattern", "query",
    "content_length", "old_text_length", "new_text_length", "replace_all", "timeout",
  ]);
  const raw = approval.arguments;
  const argumentsObject = raw && typeof raw === "object" && !Array.isArray(raw)
    ? raw as Record<string, unknown>
    : {};
  return Object.entries(argumentsObject).filter(([key]) => allowed.has(key)).map(([key, value]) => [
    key,
    typeof value === "string" ? truncateApprovalValue(value, key === "command" ? 240 : 180) : value,
  ]);
}

function truncateApprovalValue(value: string, max: number): string {
  const normalized = value.replace(/\s+/gu, " ").trim();
  return normalized.length > max ? `${normalized.slice(0, max)}…` : normalized;
}
