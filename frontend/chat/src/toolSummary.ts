import type { ApprovalRequest, ToolActivity } from "./types";

export type ToolDisplayStatus = "running" | "completed" | "error" | "interrupted" | "unknown" | "pending";

export function toolDisplayStatus(tool: ToolActivity, pending = false): ToolDisplayStatus {
  if (tool.status === "completed") return "completed";
  if (["error", "rejected", "expired", "unavailable"].includes(tool.status)) return "error";
  if (["interrupted", "cancelled"].includes(tool.status)) return "interrupted";
  if (pending || tool.approvalState === "pending" || tool.approvalState === "submitting") return "pending";
  return tool.status === "running" ? "running" : "unknown";
}

/** 进度只统计成功项；并行执行与失败同时发生时，两种事实都保留。 */
export function deriveToolSummary(tools: ToolActivity[], approvals: ApprovalRequest[]) {
  const pendingIds = new Set(approvals.map((approval) => approval.call_id));
  const counts = { running: 0, completed: 0, error: 0, interrupted: 0, unknown: 0, pending: 0 };
  for (const tool of tools) counts[toolDisplayStatus(tool, pendingIds.has(tool.callId))] += 1;
  const toolIds = new Set(tools.map((tool) => tool.callId));
  counts.pending += approvals.filter((approval) => !toolIds.has(approval.call_id)).length;
  const total = tools.length + approvals.filter((approval) => !toolIds.has(approval.call_id)).length;
  const status: ToolDisplayStatus = counts.running ? "running" : counts.pending ? "pending"
    : counts.error ? "error" : counts.interrupted ? "interrupted" : counts.unknown ? "unknown" : "completed";
  const active = tools.find((tool) => toolDisplayStatus(tool, pendingIds.has(tool.callId)) === "running");
  const notes = [
    active ? `正在执行 ${active.name}` : "",
    counts.pending ? `${counts.pending} 项等待授权` : "",
    counts.error ? `${counts.error} 项失败` : "",
    counts.interrupted ? `${counts.interrupted} 项已中断` : "",
    counts.unknown ? `${counts.unknown} 项状态未知` : "",
    `${counts.completed} / ${total} 已完成`,
  ].filter(Boolean);
  const errors = tools.filter((tool) => toolDisplayStatus(tool) === "error").map((tool) => ({
    callId: tool.callId,
    text: `${tool.name}：${(tool.resultPreview || tool.errorCode || "执行失败，展开查看详情").replace(/\s+/gu, " ").slice(0, 180)}`,
  }));
  return { status, label: notes.join(" · "), counts, errors };
}
