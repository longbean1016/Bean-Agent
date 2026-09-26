import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { ToolTimeline } from "./ToolTimeline";
import type { ApprovalRequest, ToolActivity } from "./types";

afterEach(cleanup);
const running: ToolActivity = { callId: "call", name: "read_file", status: "running", arguments: { path: "sample.txt" }, resultPreview: "" };
const approval: ApprovalRequest = { id: "approval", call_id: "call", turn_id: "turn", session_id: "web:test", tool_name: "read_file",
  operation: "读取文件", arguments: {}, reason: "需要确认", requested_mode: "read-only", state: "pending", created_at: "2026-01-01T00:00:00Z" };
function props() { return { tools: [running], approvals: [] as ApprovalRequest[], approvalDecisionRequests: {}, disclosure: new Map<string, boolean>(), disclosureKey: "test" }; }

it("每项直接可见，默认收起，完成与新增工具不改变手动选择", () => {
  const initial = props();
  const { container, rerender } = render(<ToolTimeline {...initial} />);
  expect(screen.queryByRole("button", { name: /工具调用/ })).not.toBeInTheDocument();
  const step = screen.getByRole("button", { name: /^read_file/ });
  expect(step).toHaveAttribute("aria-expanded", "false");
  expect(container.querySelector(".tool-running-spinner")).not.toBeNull();
  fireEvent.click(step);
  rerender(<ToolTimeline {...initial} tools={[{ ...running, status: "completed", resultPreview: "完成结果" }, { ...running, callId: "second" }]} />);
  expect(step).toHaveAttribute("aria-expanded", "true");
  expect(screen.getByText("完成结果")).toBeVisible();
  expect(screen.getAllByRole("button", { name: /^read_file/ })[1]).toHaveAttribute("aria-expanded", "false");
});

it("卸载再挂载恢复单项选择，其他会话不继承", () => {
  const initial = props();
  const first = render(<ToolTimeline {...initial} />);
  fireEvent.click(screen.getByRole("button", { name: /^read_file/ }));
  first.unmount();
  const second = render(<ToolTimeline {...initial} />);
  expect(screen.getByRole("button", { name: /^read_file/ })).toHaveAttribute("aria-expanded", "true");
  second.unmount();
  render(<ToolTimeline {...initial} disclosureKey="other-session" />);
  expect(screen.getByRole("button", { name: /^read_file/ })).toHaveAttribute("aria-expanded", "false");
});

it("单项收起时保留原审批卡且不转圈，提交锁即时更新", () => {
  const initial = props();
  const decide = vi.fn();
  const { container, rerender } = render(<ToolTimeline {...initial} approvals={[approval]} onApprovalDecision={decide} />);
  expect(screen.getByRole("button", { name: /^read_file/ })).toHaveAttribute("aria-expanded", "false");
  expect(container.querySelector(".tool-running-spinner")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "仅允许本次" }));
  expect(decide).toHaveBeenCalledExactlyOnceWith(approval, "allowed-once");
  rerender(<ToolTimeline {...initial} approvals={[approval]} onApprovalDecision={decide} approvalDecisionRequests={{ approval: "request" }} />);
  expect(screen.getByRole("button", { name: "提交中…" })).toBeDisabled();
});

it("失败和成功详情仅在各自展开时出现", () => {
  render(<ToolTimeline {...props()} tools={[
    { ...running, status: "completed", resultPreview: "读取成功" },
    { ...running, name: "shell", callId: "failed", arguments: { command: "first" }, status: "error", resultPreview: "命令失败详情" },
  ]} />);
  expect(screen.queryByText("命令失败详情")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: /^shell/ }));
  expect(screen.getByText("命令失败详情")).toBeVisible();
  expect(screen.queryByText("读取成功")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: /^shell/ }));
  fireEvent.click(screen.getByRole("button", { name: /^read_file/ }));
  expect(screen.queryByText("命令失败详情")).not.toBeInTheDocument();
  expect(screen.getByText("读取成功")).toBeVisible();
});

it("工具图标不随状态替换，右侧展示转圈、勾或失败叉", () => {
  const initial = props();
  const { container, rerender } = render(<ToolTimeline {...initial} />);
  expect(container.querySelector(".tool-icon .lucide-file-text")).not.toBeNull();
  expect(container.querySelector(".tool-status-label .tool-running-spinner")).not.toBeNull();
  rerender(<ToolTimeline {...initial} tools={[{ ...running, status: "completed" }]} />);
  expect(container.querySelector(".tool-icon .lucide-file-text")).not.toBeNull();
  expect(container.querySelectorAll(".tool-status-completed .lucide-check")).toHaveLength(1);
  rerender(<ToolTimeline {...initial} tools={[{ ...running, name: "web_search", status: "error" }]} />);
  expect(container.querySelector(".tool-icon .lucide-earth")).not.toBeNull();
  expect(container.querySelector(".tool-status-error .lucide-x")).not.toBeNull();
});

it("技能只展示技能名与专属图标，生命周期文案真实", () => {
  const initial = props();
  const tool: ToolActivity = { ...running, name: "load_skill", arguments: { name: "weather" } };
  const { container, rerender } = render(<ToolTimeline {...initial} tools={[tool]} />);
  expect(screen.getByText("正在加载技能")).toBeVisible();
  expect(container.querySelector(".lucide-wand-sparkles")).not.toBeNull();
  rerender(<ToolTimeline {...initial} tools={[{ ...tool, status: "completed", resultPreview: "技能正文" }]} />);
  expect(screen.getByText("使用了技能")).toBeVisible();
  expect(screen.getByText("weather")).toBeVisible();
  expect(screen.queryByText("load_skill")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: /使用了技能/ }));
  expect(screen.getByText("技能正文")).toBeVisible();
  rerender(<ToolTimeline {...initial} tools={[{ ...tool, status: "error", resultPreview: "加载失败详情" }]} />);
  expect(screen.getByText("技能加载失败")).toBeVisible();
});

it("显式记忆空检索保留真实调用，结构化摘要不受原输出截断影响", () => {
  const initial = props();
  const tool: ToolActivity = { ...running, name: "recall_memory", status: "completed", arguments: { query: "跑步" },
    resultPreview: '{"count":0,"items":[]}' };
  const { rerender } = render(<ToolTimeline {...initial} tools={[tool]} />);
  expect(screen.getByText("找到 0 条")).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: /^recall_memory/ }));
  expect(screen.getByText("未找到相关记忆")).toBeVisible();
  rerender(<ToolTimeline {...initial} tools={[{ ...tool, resultPreview: '{"items":[已截断',
    memoryResult: { count: 1, items: [{ id: "m1", summary: "喜欢夜跑" }] } }]} />);
  expect(screen.getByText("喜欢夜跑")).toBeVisible();
  expect(screen.queryByText(/已截断/)).not.toBeInTheDocument();
});
