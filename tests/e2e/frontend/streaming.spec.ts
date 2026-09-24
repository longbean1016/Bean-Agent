import { expect, test, type Page, type WebSocketRoute } from "@playwright/test";

const sessionId = "web:stream-ui";
const turnId = "stream-ui-turn";

async function openSimulation(page: Page, historyTurns = 0): Promise<WebSocketRoute> {
  let connect!: (socket: WebSocketRoute) => void;
  const connected = new Promise<WebSocketRoute>((resolve) => { connect = resolve; });
  await page.routeWebSocket("**/ws", (socket) => connect(socket));
  await page.route("**/api/chat/sessions/*/messages**", (route) => route.fulfill({ json: {
    items: [
      ...Array.from({ length: historyTurns }, (_, index) => [
        { id: `user-${index}`, seq: index * 2, role: "user", content: `模拟历史问题 ${index}`, turn_id: `history-${index}` },
        { id: `answer-${index}`, seq: index * 2 + 1, role: "assistant", content: "模拟历史段落。\n\n".repeat(8), turn_id: `history-${index}` },
      ]).flat(),
      { id: "user-current", seq: historyTurns * 2, role: "user", content: "帮我查询天气，整理出门建议。", turn_id: turnId },
    ], has_more: false,
  } }));
  await page.goto("/chat/stream-ui");
  await expect(page.getByRole("button", { name: "已连接" })).toBeVisible();
  await expect(page.locator(".user-text").filter({ hasText: "帮我查询天气，整理出门建议。" })).toBeVisible();
  const socket = await connected;
  socket.send(JSON.stringify({ type: "turn.started", session_id: sessionId, turn_id: turnId }));
  return socket;
}

test("工具默认收起并保留审批，失败输出只在对应单项展开后显示", async ({ page }, testInfo) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  const socket = await openSimulation(page);
  const send = (frame: object) => socket.send(JSON.stringify({ session_id: sessionId, turn_id: turnId, ...frame }));
  send({ type: "react.tool.started", call_id: "read", tool_name: "read_file", arguments: { path: "weather.json" } });
  send({ type: "react.tool.completed", call_id: "read", tool_name: "read_file", status: "completed", result_preview: "已读取查询配置", duration_ms: 100 });
  send({ type: "react.tool.started", call_id: "weather", tool_name: "web_search", arguments: { query: "深圳南山天气" } });
  const group = page.locator(".tool-group-trigger");
  await expect(group).toHaveAttribute("aria-expanded", "false");
  await expect(group).toContainText("1 / 2 已完成");
  await expect(group.locator(".tool-group-icon .lucide-wrench")).toBeVisible();
  await expect(page.locator(".tool-group")).toHaveCSS("border-top-width", "0px");
  await expect(page.locator(".tool-group")).toHaveCSS("background-color", "rgba(0, 0, 0, 0)");
  await expect(page.locator(".tool-running-spinner")).toBeVisible();
  await group.focus();
  await page.keyboard.press("Enter");
  const tool = page.locator(".tool-trigger").filter({ hasText: "web_search" });
  await expect(tool).toHaveAttribute("aria-expanded", "false");
  await expect(tool.locator(".tool-status-label")).toBeVisible();
  await expect(tool.locator(".tool-icon .lucide-earth")).toBeVisible();
  await expect(tool.locator(".tool-status-label .tool-running-spinner")).toBeVisible();
  await expect(page.locator(".tool-trigger").filter({ hasText: "read_file" }).locator(".tool-status-label .lucide-check")).toBeVisible();
  await tool.click();
  await expect(tool).toHaveAttribute("aria-expanded", "true");
  send({ type: "answer.delta", delta: "正在整理天气信息。" });
  await expect(page.getByText("正在整理天气信息。", { exact: true })).toBeVisible();
  await expect(group).toHaveAttribute("aria-expanded", "true");
  await expect(tool).toHaveAttribute("aria-expanded", "true");
  await tool.click();
  await page.screenshot({ path: `.pytest_artifacts/tools-running-${testInfo.project.name}.png`, animations: "disabled" });
  await page.getByRole("button", { name: "深色", exact: true }).click();
  await page.screenshot({ path: `.pytest_artifacts/tools-dark-${testInfo.project.name}.png`, animations: "disabled" });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await expect(group.locator(".tool-running-spinner")).toHaveCSS("animation-name", "none");
  await expect(tool.locator(".tool-running-spinner")).toHaveCSS("animation-name", "none");
  await group.click();
  send({ type: "approval.requested", approval: { id: "approval", call_id: "weather", session_id: sessionId, turn_id: turnId,
    tool_name: "web_search", operation: "联网查询", arguments: {}, summary: "允许本次联网查询", reason: "测试审批", requested_mode: "read-only", state: "pending", created_at: "2026-09-25T00:00:00Z" } });
  await expect(group).toHaveAttribute("aria-expanded", "false");
  await expect(page.locator(".tool-running-spinner")).toHaveCount(0);
  await expect(page.getByLabel("待处理权限审批")).toBeVisible();
  await expect(page.getByRole("button", { name: "仅允许本次" })).toBeEnabled();
  send({ type: "approval.resolved", approval_id: "approval", decision: "allowed-once", call_id: "weather", request_id: "mock" });
  send({ type: "react.tool.completed", call_id: "weather", tool_name: "web_search", status: "error", result_preview: "测试错误：天气服务暂时不可用", duration_ms: 500 });
  await expect(page.getByLabel("待处理权限审批")).toHaveCount(0);
  await expect(page.getByText("测试错误：天气服务暂时不可用", { exact: true })).toHaveCount(0);
  await expect(group.locator(".tool-group-state")).toHaveText("1 / 2 已完成");
  await expect(group.locator(".tool-status-error")).toHaveText("1 项失败");
  await expect(group.locator(".tool-status-error .lucide-x")).toBeVisible();
  await expect(group.locator(".lucide-check")).toHaveCount(0);
  await page.screenshot({ path: `.pytest_artifacts/tools-error-${testInfo.project.name}.png`, animations: "disabled" });
  await expect(group).toHaveAttribute("aria-expanded", "false");
  await expect(page.locator(".tool-running-spinner")).toHaveCount(0);
  await group.click();
  await expect(tool).toHaveAttribute("aria-expanded", "false");
  await expect(page.getByText("测试错误：天气服务暂时不可用", { exact: true })).toHaveCount(0);
  await tool.click();
  await expect(tool.locator("..").locator(".tool-result-preview")).toHaveText("测试错误：天气服务暂时不可用");
  await expect(page.getByText("已读取查询配置", { exact: true })).toHaveCount(0);
  await tool.click();
  await expect(page.getByText("测试错误：天气服务暂时不可用", { exact: true })).toHaveCount(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  expect(errors).toEqual([]);
});

test("长历史流式输出尾字完整，向上阅读不被拉回，结束后代码复制可用", async ({ page }) => {
  const socket = await openSimulation(page, 30);
  const send = (frame: object) => socket.send(JSON.stringify({ session_id: sessionId, turn_id: turnId, ...frame }));
  const first = "第一段模拟内容。\n\n".repeat(40);
  send({ type: "answer.delta", delta: first });
  const scroller = page.locator(".conversation-scroll");
  await expect(page.locator(".assistant-message").last()).toContainText("第一段模拟内容");
  await expect.poll(() => scroller.evaluate((element) => element.scrollHeight - element.clientHeight - element.scrollTop)).toBeLessThan(100);
  await scroller.hover();
  await page.mouse.wheel(0, -450);
  await expect(page.getByRole("button", { name: "回到最新消息" })).toBeVisible();
  const top = await scroller.evaluate((element) => element.scrollTop);
  const rest = "\n\n后续内容不会强制拉回。".repeat(20);
  for (const char of rest) send({ type: "answer.delta", delta: char });
  send({ type: "answer.delta", delta: "\n\n```js\nconst value = 1;\n```\n\n完整尾字" });
  send({ type: "message.final", content: first + rest + "\n\n```js\nconst value = 1;\n```\n\n完整尾字", thinking: "", media: [] });
  await expect.poll(() => scroller.evaluate((element) => element.scrollTop)).toBeLessThan(top + 80);
  await page.getByRole("button", { name: "回到最新消息" }).click();
  await expect(page.getByText("完整尾字", { exact: true })).toBeVisible();
  await expect(page.getByTitle("复制代码")).toBeEnabled();
  await expect.poll(() => scroller.evaluate((element) => element.scrollHeight - element.clientHeight - element.scrollTop)).toBeLessThan(100);
});
