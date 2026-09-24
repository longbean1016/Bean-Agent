import { expect, it } from "vitest";
import { deriveToolSummary, toolDisplayStatus } from "./toolSummary";
import type { ApprovalRequest, ToolActivity, ToolStatus } from "./types";

const tool = (status: ToolStatus, callId = status): ToolActivity => ({ callId, name: "shell", status, resultPreview: "" });

it("成功、失败、中断和未知状态分开统计", () => {
  const summary = deriveToolSummary([tool("completed"), tool("error"), tool("interrupted"), tool("unknown")], []);
  expect(summary.label).toBe("1 项失败 · 1 项已中断 · 1 项状态未知 · 1 / 4 已完成");
  expect(summary.status).toBe("error");
});
it("并行调用仍在执行时保留失败提示，并支持动态增加工具", () => {
  const summary = deriveToolSummary([tool("running"), tool("completed"), { ...tool("error"), resultPreview: "测试错误" }], []);
  expect(summary.status).toBe("running");
  expect(summary.label).toContain("1 项失败 · 1 / 3 已完成");
  expect(summary.errors[0].text).toContain("测试错误");
});
it("待审批工具即使协议状态为 running 也不转圈，孤立审批不重复计数", () => {
  const approval = { call_id: "running" } as ApprovalRequest;
  expect(deriveToolSummary([tool("running")], [approval]).status).toBe("pending");
  expect(deriveToolSummary([], [approval]).label).toBe("1 项等待授权 · 0 / 1 已完成");
  expect(toolDisplayStatus({ ...tool("running"), approvalState: "pending" })).toBe("pending");
  expect(toolDisplayStatus({ ...tool("running"), approvalState: "submitting" })).toBe("pending");
});
it.each(["rejected", "expired", "unavailable"] as const)("%s 不误判成功", (status) => {
  expect(deriveToolSummary([tool(status)], []).status).toBe("error");
});
it("终态停止转圈且未知状态保持保守", () => {
  expect(deriveToolSummary([tool("completed")], []).status).toBe("completed");
  expect(deriveToolSummary([tool("cancelled")], []).status).toBe("interrupted");
  expect(deriveToolSummary([tool("unknown")], []).status).toBe("unknown");
});
