import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { ToolTimeline } from "./ToolTimeline";
import type { ApprovalRequest, ToolActivity } from "./types";

afterEach(cleanup);
const running: ToolActivity = { callId: "call", name: "read_file", status: "running", arguments: { path: "sample.txt" }, resultPreview: "" };
const approval: ApprovalRequest = { id: "approval", call_id: "call", turn_id: "turn", session_id: "web:test", tool_name: "read_file",
  operation: "读取文件", arguments: {}, reason: "需要确认", requested_mode: "read-only", state: "pending", created_at: "2026-01-01T00:00:00Z" };
function props() { return { tools: [running], approvals: [] as ApprovalRequest[], approvalDecisionRequests: {}, disclosure: new Map<string, boolean>(), disclosureKey: "test" }; }

it("执行中默认收起且显示转圈，手动选择在完成和新增工具后保持", () => {
  const initial = props();
  const { container, rerender } = render(<ToolTimeline {...initial} />);
  const trigger = screen.getByRole("button", { name: /工具调用/ });
  expect(trigger).toHaveAttribute("aria-expanded", "false");
  expect(container.querySelector(".tool-running-spinner")).not.toBeNull();
  expect(screen.queryByText("sample.txt")).not.toBeInTheDocument();
  fireEvent.click(trigger);
  const step = screen.getByRole("button", { name: /^read_file/ });
  expect(step).toHaveAttribute("aria-expanded", "false");
  fireEvent.click(step);
  rerender(<ToolTimeline {...initial} tools={[{ ...running, status: "completed", durationMs: 100, resultPreview: "完成结果" }]} />);
  expect(trigger).toHaveAttribute("aria-expanded", "true");
  expect(step).toHaveAttribute("aria-expanded", "true");
  expect(screen.getByText("完成结果")).toBeVisible();
  expect(container.querySelector(".tool-running-spinner")).toBeNull();
  fireEvent.click(trigger);
  rerender(<ToolTimeline {...initial} tools={[{ ...running, status: "completed" }, { ...running, callId: "second" }]} />);
  expect(trigger).toHaveAttribute("aria-expanded", "false");
  fireEvent.click(trigger);
  expect(screen.getAllByRole("button", { name: /^read_file/ })[0]).toHaveAttribute("aria-expanded", "true");
  expect(screen.getAllByRole("button", { name: /^read_file/ })[1]).toHaveAttribute("aria-expanded", "false");
});

it("虚拟列表卸载再挂载后恢复手动展开，其他会话不继承", () => {
  const initial = props();
  const first = render(<ToolTimeline {...initial} />);
  fireEvent.click(screen.getByRole("button", { name: /工具调用/ }));
  fireEvent.click(screen.getByRole("button", { name: /^read_file/ }));
  first.unmount();
  const second = render(<ToolTimeline {...initial} />);
  expect(screen.getByRole("button", { name: /工具调用/ })).toHaveAttribute("aria-expanded", "true");
  expect(screen.getByRole("button", { name: /^read_file/ })).toHaveAttribute("aria-expanded", "true");
  second.unmount();
  render(<ToolTimeline {...initial} disclosureKey="other-session" />);
  expect(screen.getByRole("button", { name: /工具调用/ })).toHaveAttribute("aria-expanded", "false");
});

it("组和单项都收起时保留原审批卡且不转圈，提交锁即时更新", () => {
  const initial = props();
  const decide = vi.fn();
  const { container, rerender } = render(<ToolTimeline {...initial} approvals={[approval]} onApprovalDecision={decide} />);
  expect(screen.getByRole("button", { name: /工具调用/ })).toHaveAttribute("aria-expanded", "false");
  expect(container.querySelector(".tool-running-spinner")).toBeNull();
  expect(screen.getByLabelText("待处理权限审批")).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "仅允许本次" }));
  expect(decide).toHaveBeenCalledExactlyOnceWith(approval, "allowed-once");
  rerender(<ToolTimeline {...initial} approvals={[approval]} onApprovalDecision={decide} approvalDecisionRequests={{ approval: "request" }} />);
  expect(screen.getByRole("button", { name: "提交中…" })).toBeDisabled();
});

it("错误摘要不因默认收起而隐藏，不挂载参数详情", () => {
  const initial = props();
  render(<ToolTimeline {...initial} tools={[{ ...running, status: "error", resultPreview: "读取失败：文件不存在" }]} />);
  expect(screen.getByRole("button", { name: /工具调用/ })).toHaveAttribute("aria-expanded", "false");
  expect(screen.getByText(/读取失败：文件不存在/)).toBeVisible();
  expect(screen.queryByText("目标")).not.toBeInTheDocument();
});

it("工具类型图标不随状态替换，右侧独立展示转圈和完成勾", () => {
  const initial = props();
  const { container, rerender } = render(<ToolTimeline {...initial} />);
  const trigger = screen.getByRole("button", { name: /工具调用/ });
  expect(trigger.querySelector(".tool-group-icon .lucide-wrench")).not.toBeNull();
  expect(trigger.querySelector(".tool-status-label .tool-running-spinner")).not.toBeNull();
  fireEvent.click(trigger);
  const step = screen.getByRole("button", { name: /^read_file/ });
  expect(step.querySelector(".tool-icon .lucide-file-text")).not.toBeNull();
  expect(step.querySelector(".tool-status-label .tool-running-spinner")).not.toBeNull();
  rerender(<ToolTimeline {...initial} tools={[{ ...running, status: "completed" }]} />);
  expect(trigger.querySelector(".tool-group-icon .lucide-wrench")).not.toBeNull();
  expect(step.querySelector(".tool-icon .lucide-file-text")).not.toBeNull();
  expect(container.querySelectorAll(".tool-status-completed .lucide-check")).toHaveLength(2);
  expect(container.querySelector(".tool-running-spinner")).toBeNull();
  expect(screen.queryByText("完成态 · 展开预览")).not.toBeInTheDocument();
  expect(screen.queryByText("执行中 · 默认收起")).not.toBeInTheDocument();
});

it("部分失败时摘要显示失败数量，失败叉不覆盖浏览器图标", () => {
  const initial = props();
  const { container } = render(<ToolTimeline {...initial} tools={[
    { ...running, status: "completed" },
    { ...running, callId: "failed", name: "web_search", status: "error", resultPreview: "网络暂时不可用" },
  ]} />);
  const trigger = screen.getByRole("button", { name: /工具调用/ });
  expect(trigger.querySelector(".tool-status-error")).toHaveTextContent("1 项失败");
  expect(trigger.querySelector(".lucide-check")).toBeNull();
  expect(trigger.querySelector(".tool-group-icon .lucide-wrench")).not.toBeNull();
  expect(screen.getByText(/网络暂时不可用/)).toBeVisible();
  fireEvent.click(trigger);
  const step = screen.getByRole("button", { name: /^web_search/ });
  expect(step.querySelector(".tool-icon .lucide-earth")).not.toBeNull();
  expect(step.querySelector(".tool-status-error .lucide-x")).not.toBeNull();
  expect(container.querySelector(".tool-running-spinner")).toBeNull();
});
