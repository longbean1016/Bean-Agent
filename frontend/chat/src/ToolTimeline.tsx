import * as Collapsible from "@radix-ui/react-collapsible";
import { Check, ChevronDown, CircleStop, CircleHelp, Clock3, FileText, FilePenLine, FolderOpen, Globe2, LoaderCircle, MessageCircle, PlugZap, SquareTerminal, Wrench, X } from "lucide-react";
import { memo, useEffect, useState } from "react";
import { ApprovalCard } from "./SandboxControls";
import type { ApprovalRequest, ToolActivity } from "./types";
import { deriveToolSummary, toolDisplayStatus, type ToolDisplayStatus } from "./toolSummary";
import "./toolTimeline.css";

/**
 * 工具类型始终保留在左侧，执行状态独立放在右侧，避免终态覆盖工具语义。
 */
type ToolVisualKind = "terminal" | "browser" | "tool-loader" | "file-read" | "file-write" | "file-edit" | "directory" | "message" | "mcp" | "generic";

function toolVisualKind(name: string): ToolVisualKind {
  const normalized = name.trim().toLowerCase().replace(/[\s.:-]+/gu, "_");
  if (/^(shell|exec|run|terminal|command)(?:_|$)/u.test(normalized)) return "terminal";
  if (normalized === "web_search" || normalized === "web_fetch" || normalized === "browser" || normalized.startsWith("browser_") || normalized.startsWith("web_")) return "browser";
  if (normalized === "tool_search" || normalized === "load_skill" || normalized.includes("skill")) return "tool-loader";
  if (normalized === "read_file" || normalized.includes("read_file") || normalized.includes("get_file") || normalized.includes("fetch_file")) return "file-read";
  if (normalized === "write_file" || normalized.includes("write_file") || normalized.includes("create_file") || normalized.includes("upload_file")) return "file-write";
  if (normalized === "edit_file" || normalized.includes("edit_file") || normalized.includes("patch_file") || normalized.includes("update_file")) return "file-edit";
  if (normalized === "list_dir" || normalized.includes("list_dir") || normalized.includes("list_directory") || normalized.includes("directory")) return "directory";
  if (normalized === "send_message" || normalized === "notify" || normalized.startsWith("message_") || normalized.startsWith("send_")) return "message";
  if (normalized.startsWith("mcp_") || normalized.startsWith("plugin_") || normalized.includes("connector")) return "mcp";
  return "generic";
}

function toolIcon(kind: ToolVisualKind, size = 16) {
  if (kind === "terminal") return <SquareTerminal size={size} />;
  if (kind === "browser") return <Globe2 size={size} />;
  if (kind === "file-read") return <FileText size={size} />;
  if (kind === "file-write" || kind === "file-edit") return <FilePenLine size={size} />;
  if (kind === "directory") return <FolderOpen size={size} />;
  if (kind === "message") return <MessageCircle size={size} />;
  if (kind === "mcp") return <PlugZap size={size} />;
  return <Wrench size={size} />;
}

const toolStatusLabels: Record<ToolDisplayStatus, string> = {
  running: "执行中", completed: "完成", error: "失败", interrupted: "已中断", unknown: "状态未知", pending: "等待授权",
};

function ToolStatus({ status, label }: { status: ToolDisplayStatus; label?: string }) {
  const Icon = status === "running" ? LoaderCircle : status === "completed" ? Check
    : status === "error" ? X : status === "pending" ? Clock3 : status === "interrupted" ? CircleStop : CircleHelp;
  return <span className={`tool-status-label tool-status-${status}`}>
    <Icon size={13} aria-hidden="true" className={status === "running" ? "tool-running-spinner" : undefined} />
    {label || toolStatusLabels[status]}
  </span>;
}

function summarizeMessageRecipient(tool: ToolActivity): string {
  if (!tool.arguments || typeof tool.arguments !== "object" || Array.isArray(tool.arguments)) return "";
  const args = tool.arguments as Record<string, unknown>;
  for (const key of ["to", "recipient", "channel", "thread", "target", "name"]) {
    const value = args[key];
    if (typeof value === "string" && value.trim()) return truncateToolText(value.trim(), 72);
  }
  return "";
}

function toolActionLabel(tool: ToolActivity, kind: ToolVisualKind): string {
  if (tool.status !== "completed") {
    const labels: Record<ToolVisualKind, string> = { terminal: "运行命令", browser: "查询信息", "tool-loader": "加载工具",
      "file-read": "读取文件", "file-write": "写入文件", "file-edit": "编辑文件", directory: "查看目录", message: "发送消息", mcp: "调用 MCP 工具", generic: "运行工具" };
    return labels[kind];
  }
  if (kind === "terminal") return "运行了命令";
  if (kind === "browser") return "已使用浏览器运行了命令";
  if (kind === "tool-loader") return "加载了工具";
  if (kind === "file-read") return "读取了文件";
  if (kind === "file-write") return "写入了文件";
  if (kind === "file-edit") return "编辑了文件";
  if (kind === "directory") return "查看了目录";
  if (kind === "message") {
    const recipient = summarizeMessageRecipient(tool);
    return recipient ? `已向 ${recipient} 发送消息` : "发送了消息";
  }
  if (kind === "mcp") return "调用了 MCP 工具";
  return "运行了工具";
}

export type ToolDisclosureState = Map<string, boolean>;

type ToolTimelineProps = {
  tools: ToolActivity[];
  approvals: ApprovalRequest[];
  approvalDecisionRequests: Record<string, string>;
  onApprovalDecision?: (approval: ApprovalRequest, decision: "allowed-once" | "rejected") => void;
  disclosure: ToolDisclosureState;
  disclosureKey: string;
};

// 虚拟列表卸载消息后仍保留当前会话内的手动选择，工具状态变化不改写展开状态。
function useDisclosure(store: ToolDisclosureState, key: string): [boolean, (open: boolean) => void] {
  const [open, setOpen] = useState(() => store.get(key) ?? false);
  return [open, (value) => { store.set(key, value); setOpen(value); }];
}

export const ToolTimeline = memo(function ToolTimeline({
  tools, approvals, approvalDecisionRequests, onApprovalDecision, disclosure, disclosureKey,
}: ToolTimelineProps) {
  const [groupOpen, setGroupOpen] = useDisclosure(disclosure, disclosureKey);
  const [now, setNow] = useState(() => Date.now());
  const summary = deriveToolSummary(tools, approvals);
  // 收起时不为隐藏的耗时详情刷新组件；转圈只由 CSS 驱动。
  const hasRunningTimer = groupOpen && tools.some((tool) => toolDisplayStatus(tool) === "running" && tool.startedAt);
  useEffect(() => {
    if (!hasRunningTimer) return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [hasRunningTimer]);
  const pendingByCall = new Map(approvals.map((approval) => [approval.call_id, approval]));
  return (
    <Collapsible.Root className={`tool-timeline tool-group ${summary.status}`} open={groupOpen} onOpenChange={setGroupOpen}>
      <Collapsible.Trigger className="tool-group-trigger" aria-label={`工具调用 · ${summary.label}，${groupOpen ? "收起" : "展开"}工具详情`}>
        <span className="tool-group-icon" aria-hidden="true">
          <Wrench size={16} />
        </span>
        <span className="tool-group-summary">
          <strong>工具调用</strong>
          <span className="tool-group-state" aria-live="polite" aria-atomic="true">{summary.label}</span>
        </span>
        <ToolStatus status={summary.status} label={summary.status === "error" ? `${summary.counts.error} 项失败` : undefined} />
        <ChevronDown size={14} aria-hidden="true" />
      </Collapsible.Trigger>
      {summary.errors.length ? <div className="tool-group-errors" role="status">
        {summary.errors.map((error) => <p key={error.callId}>{error.text}</p>)}
      </div> : null}
      {/* 审批沿用原卡片和消息内位置，独立于两层折叠，收起或懒挂载均不影响操作。 */}
      {approvals.map((approval, index) => (
        <div className="tool-orphan-approval" key={approval.id}>
          <ApprovalCard approval={approval} queuePosition={index + 1} queueTotal={approvals.length}
            submitting={Boolean(approvalDecisionRequests[approval.id])}
            onDecide={(decision) => onApprovalDecision?.(approval, decision)} inline />
        </div>
      ))}
      <Collapsible.Content className="tool-group-content">
        {tools.map((tool) => (
          <ToolStep key={tool.callId} tool={tool} now={now} approval={pendingByCall.get(tool.callId)}
            open={disclosure.get(`${disclosureKey}:${tool.callId}`) ?? false}
            onOpenChange={(open) => disclosure.set(`${disclosureKey}:${tool.callId}`, open)} />
        ))}
      </Collapsible.Content>
    </Collapsible.Root>
  );
}, (previous, next) => previous.tools === next.tools
  && previous.disclosure === next.disclosure && previous.disclosureKey === next.disclosureKey
  && previous.onApprovalDecision === next.onApprovalDecision
  && previous.approvals.length === next.approvals.length
  && previous.approvals.every((approval, index) => approval === next.approvals[index]
    && previous.approvalDecisionRequests[approval.id] === next.approvalDecisionRequests[approval.id]));

function ToolStep({ tool, now, approval, open, onOpenChange }: {
  tool: ToolActivity; now: number; approval?: ApprovalRequest;
  open: boolean; onOpenChange: (open: boolean) => void;
}) {
  const [stepOpen, setStepOpen] = useState(open);
  const kind = toolVisualKind(tool.name);
  const actionLabel = toolActionLabel(tool, kind);
  const displayStatus = toolDisplayStatus(tool, Boolean(approval));
  const statusLabel = displayStatus === "pending"
    ? "等待授权"
    : tool.approvalState === "allowed-once" && tool.status === "running" ? "执行中 · 已允许本次"
      : tool.approvalState === "rejected" ? "已拒绝"
        : tool.approvalState === "cancelled" ? "已取消"
          : tool.approvalState === "expired" ? "授权超时"
            : tool.approvalState === "unavailable" ? "授权不可用"
    : tool.status === "running" ? "执行中"
      : tool.status === "error" || tool.status === "rejected" ? "失败"
        : tool.status === "interrupted" || tool.status === "cancelled" ? "已中断"
          : tool.status === "expired" ? "已超时"
            : tool.status === "unavailable" ? "不可用"
              : tool.status === "unknown" ? "状态未知" : "完成";
  const duration = displayStatus === "pending" ? null : toolDurationForDisplay(tool, now);
  const target = summarizeToolTarget(tool);
  return (
    <Collapsible.Root
      className={`tool-step ${tool.status}${approval ? " approval-pending" : ""}`}
      open={stepOpen}
      onOpenChange={(nextOpen) => {
        setStepOpen(nextOpen);
        onOpenChange(nextOpen);
      }}
    >
      <Collapsible.Trigger className="tool-trigger" aria-label={`${tool.name}${target ? ` ${target}` : ""}，${actionLabel}，${statusLabel}`}>
        <span className="tool-icon" aria-hidden="true">{toolIcon(kind, 16)}</span>
        <span className="tool-action">{actionLabel}</span>
        <strong className="tool-name">{tool.name}</strong>
        {target && kind !== "message" ? <span className="tool-target">{target}</span> : null}
        <ToolStatus status={displayStatus} label={statusLabel} />
        {duration ? <time className="tool-duration">{duration}</time> : null}
        <ChevronDown size={14} aria-hidden="true" />
      </Collapsible.Trigger>
      <Collapsible.Content className="tool-detail">
        <dl className="tool-detail-grid">
          {target ? <div><dt>目标</dt><dd>{target}</dd></div> : null}
          {tool.startedAt ? <div><dt>开始</dt><dd>{formatToolTimestamp(tool.startedAt)}</dd></div> : null}
          {tool.endedAt ? <div><dt>结束</dt><dd>{formatToolTimestamp(tool.endedAt)}</dd></div> : null}
          <div><dt>耗时</dt><dd>{duration || "耗时未知"}</dd></div>
          {tool.approvalWaitMs !== undefined ? <div><dt>授权等待</dt><dd>{formatCompactDuration(tool.approvalWaitMs)}</dd></div> : null}
          {tool.executionMs !== undefined ? <div><dt>执行</dt><dd>{formatCompactDuration(tool.executionMs)}</dd></div> : null}
          {tool.approvalState && tool.approvalState !== "none" && !approval ? <div><dt>授权</dt><dd>{approvalStateLabel(tool.approvalState)}</dd></div> : null}
          {tool.resultKind ? <div><dt>结果类型</dt><dd>{tool.resultKind}</dd></div> : null}
          {tool.isTruncated ? <div><dt>输出</dt><dd>已截断</dd></div> : null}
          {tool.exitCode !== undefined ? <div><dt>退出码</dt><dd>{String(tool.exitCode)}</dd></div> : null}
          {tool.errorCode ? <div><dt>错误</dt><dd>{tool.errorCode}</dd></div> : null}
        </dl>
        {tool.resultPreview ? <p className="tool-result-preview">{truncateToolText(tool.resultPreview)}</p> : null}
        {tool.status === "unknown" ? <p className="tool-unknown-note" role="status">服务端返回了未识别的工具状态，已按保守状态展示。</p> : null}
      </Collapsible.Content>
    </Collapsible.Root>
  );
}

function approvalStateLabel(state: NonNullable<ToolActivity["approvalState"]>): string {
  if (state === "pending") return "等待确认";
  if (state === "submitting") return "提交中";
  if (state === "allowed-once") return "已允许本次";
  if (state === "rejected") return "已拒绝";
  if (state === "cancelled") return "已取消";
  if (state === "expired") return "已超时";
  if (state === "unavailable") return "不可用";
  return "无";
}

function toolDurationForDisplay(tool: ToolActivity, now: number): string | null {
  if (tool.durationMs !== undefined && Number.isFinite(tool.durationMs) && tool.durationMs >= 0) return formatCompactDuration(tool.durationMs);
  if (tool.status === "running" && tool.startedAt) {
    const start = Date.parse(tool.startedAt);
    if (Number.isFinite(start)) return `运行中 · ${formatCompactDuration(Math.max(0, now - start))}`;
  }
  return null;
}

function formatCompactDuration(durationMs: number): string {
  const milliseconds = Math.max(0, durationMs);
  const seconds = milliseconds / 1000;
  if (seconds < 1) return `${seconds.toFixed(1)} 秒`;
  if (seconds < 60) return `${seconds.toFixed(1).replace(/\.0$/, "")} 秒`;
  // 先把整秒四舍五入，再拆分分钟，避免 59.6 秒显示成“0 分 60 秒”。
  const totalSeconds = Math.max(60, Math.round(seconds));
  const minutes = Math.floor(totalSeconds / 60);
  const remainder = totalSeconds % 60;
  return remainder ? `${minutes} 分 ${remainder} 秒` : `${minutes} 分钟`;
}

function formatToolTimestamp(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "时间未知";
  return date.toLocaleString([], { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function summarizeToolTarget(tool: ToolActivity): string {
  if (!tool.arguments || typeof tool.arguments !== "object" || Array.isArray(tool.arguments)) return "";
  const args = tool.arguments as Record<string, unknown>;
  for (const key of ["path", "file_path", "source", "destination", "cwd", "pattern", "query", "skill", "command", "to", "recipient", "channel", "thread", "target", "name"]) {
    const value = args[key];
    if (typeof value !== "string" || !value.trim()) continue;
    return truncateToolText(value.trim(), key === "command" ? 72 : 96);
  }
  return "";
}

function truncateToolText(value: string, max = 500): string {
  const normalized = value.replace(/\s+/gu, " ").trim();
  return normalized.length > max ? `${normalized.slice(0, max)}…` : normalized;
}
