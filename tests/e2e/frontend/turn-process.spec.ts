import { expect, test } from "@playwright/test";

test.use({ channel: "msedge" });

test("真实组件接收过程帧、折叠终答并恢复历史，浅深色及窄屏可操作", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  const session = "web:ui-review";
  const turn = "turn-ui-review";
  let send: (frame: Record<string, unknown>) => void = () => { throw new Error("Socket 尚未连接"); };
  let rows: unknown[] = [];
  await page.route("**/api/chat/sessions/**/messages*", (route) => route.fulfill({ json: { items: rows, total: rows.length, has_more: false } }));
  await page.routeWebSocket("**/ws", (socket) => {
    send = (frame) => socket.send(JSON.stringify({ session_id: session, turn_id: turn, ...frame }));
    socket.onMessage((raw) => {
      const frame = JSON.parse(String(raw));
      if (frame.type === "session.subscribe") send({ type: "session.subscribed", request_id: frame.request_id });
    });
  });
  await page.goto("/chat/ui-review");
  await expect(page.getByRole("button", { name: "已连接" })).toBeVisible();
  send({ type: "turn.started", request_id: "ui-review" });
  const startedAt = new Date(Date.now() - 2000).toISOString();
  const memory = { id: "memory", kind: "memory", query: "今天深圳适合夜跑吗", items: [{ id: "m1", summary: "偏好晚上跑步，希望避开闷热天气。" }], duration_ms: 180 };
  send({ type: "turn.presentation", presentation: { version: 1, started_at: startedAt, parts: [memory] } });
  send({ type: "react.thinking.delta", part_id: "thinking", delta: "先查实时天气，再结合晚间跑步偏好给出建议。" });
  await expect(page.getByRole("button", { name: /正在思考/ })).toHaveAttribute("aria-expanded", "false");
  await page.getByRole("button", { name: /已检索记忆/ }).click();
  await expect(page.getByText("偏好晚上跑步，希望避开闷热天气。")).toBeVisible();
  const parts = [memory,
    { id: "thinking", kind: "thinking", text: "先查实时天气，再结合晚间跑步偏好给出建议。" },
    { id: "intro", kind: "text", text: "我先查看实时天气，再给你一个适合夜跑的时间建议。" },
    { id: "skill", kind: "tool", call_id: "skill" },
    { id: "fetch", kind: "tool", call_id: "fetch" },
  ];
  send({ type: "turn.presentation", presentation: { version: 1, started_at: startedAt, parts } });
  send({ type: "react.tool.started", call_id: "skill", tool_name: "load_skill", arguments: { skill: "weather" }, started_at: startedAt });
  send({ type: "react.tool.completed", call_id: "skill", tool_name: "load_skill", status: "completed", result_preview: "天气查询指引", duration_ms: 10 });
  send({ type: "react.tool.started", call_id: "fetch", tool_name: "web_fetch", arguments: { url: "https://example.com/weather" }, started_at: startedAt });
  await expect(page.getByRole("button", { name: /weather.*使用了技能/ })).toBeVisible();
  await expect(page.locator(".tool-running-spinner")).toBeVisible();
  send({ type: "react.tool.completed", call_id: "fetch", tool_name: "web_fetch", status: "completed", result_preview: "气温 27°C，体感 29°C，降雨概率 10%。", duration_ms: 900 });
  send({ type: "answer.delta", part_id: "answer", delta: "今晚可以考虑轻松跑。\n\n建议 **20:00 后** 出发，先观察降雨情况，注意补水。" });
  await expect(page.getByText("今晚可以考虑轻松跑。")).toBeVisible();
  const presentation = { version: 1, started_at: startedAt, final: true, parts: [...parts, { id: "answer", kind: "answer", text: "今晚可以考虑轻松跑。\n\n建议 **20:00 后** 出发，先观察降雨情况，注意补水。" }] };
  send({ type: "turn.presentation", presentation });
  await expect(page.getByRole("button", { name: /工作中/ })).toHaveAttribute("aria-expanded", "false");
  send({ type: "message.final", content: presentation.parts.at(-1)!.text, metadata: { presentation, duration_ms: 5100 } });
  await expect(page.getByRole("button", { name: "已工作 5.1 秒" })).toHaveAttribute("aria-expanded", "false");
  await page.getByRole("button", { name: "已工作 5.1 秒" }).click();
  await page.getByRole("button", { name: /web_fetch.*完成/ }).click();
  await expect(page.getByText("气温 27°C，体感 29°C，降雨概率 10%。")).toBeVisible();
  await page.getByRole("button", { name: /已检索记忆/ }).click();
  await page.locator(".turn-work-trigger").scrollIntoViewIfNeeded();
  if (test.info().project.name === "desktop") {
    await page.screenshot({ path: ".pytest_artifacts/turn-process-light.png", fullPage: true });
    await page.evaluate(() => document.documentElement.dataset.theme = "dark");
    await page.screenshot({ path: ".pytest_artifacts/turn-process-dark.png", fullPage: true });
  }
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: `.pytest_artifacts/turn-process-narrow-${test.info().project.name}.png`, fullPage: true });
  rows = [{ id: "saved", role: "assistant", content: presentation.parts.at(-1)!.text, turn_id: turn,
    metadata: { presentation, duration_ms: 5100 }, tool_chain: [{ calls: [
      { call_id: "skill", name: "load_skill", arguments: { skill: "weather" }, status: "completed", result: "天气查询指引" },
      { call_id: "fetch", name: "web_fetch", arguments: {}, status: "completed", result: "气温 27°C" },
    ] }] }];
  await page.reload();
  await expect(page.getByRole("button", { name: "已工作 5.1 秒" })).toHaveAttribute("aria-expanded", "false");
  await expect(page.getByText("今晚可以考虑轻松跑。")).toBeVisible();
  if (test.info().project.name === "desktop") await page.screenshot({ path: ".pytest_artifacts/turn-process-collapsed.png", fullPage: true });
  expect(errors).toEqual([]);
});
