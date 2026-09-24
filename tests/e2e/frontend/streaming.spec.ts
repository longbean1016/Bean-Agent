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
