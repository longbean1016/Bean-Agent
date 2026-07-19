import { expect, test, type Page } from "@playwright/test";

const browserErrors = new WeakMap<Page, string[]>();

test.beforeEach(async ({ page }) => {
  const errors: string[] = [];
  browserErrors.set(page, errors);
  page.on("console", (message) => { if (message.type() === "error") errors.push(message.text()); });
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("/");
  await expect(page.getByRole("button", { name: "已连接" })).toBeVisible();
});

test.afterEach(async ({ page }) => {
  expect(browserErrors.get(page) ?? []).toEqual([]);
});

test("聚合流式回复、工具状态并用 final 覆盖草稿", async ({ page }) => {
  await page.getByPlaceholder("输入消息，或附加文本与图片").fill("执行完整前端测试");
  await page.getByRole("button", { name: "发送" }).click();

  await expect(page.getByText("list_dir")).toBeVisible();
  await expect(page.getByText("完成", { exact: true })).toBeVisible();
  await expect(page.getByText("最终内容", { exact: true })).toBeVisible();
  await expect(page.getByText("流式草稿", { exact: true })).toHaveCount(0);
  await expect(page.locator("pre code")).toContainText("print");
  await page.getByRole("button", { name: /思考完成/ }).click();
  await expect(page.getByText("已经分析用户请求")).toBeVisible();
  const codeLayout = await page.locator("pre code").evaluate((codeElement) => {
    const lines = Array.from(codeElement.children);
    return {
      fontFamily: getComputedStyle(codeElement).fontFamily,
      lineDisplays: lines.map((line) => getComputedStyle(line).display),
    };
  });
  expect(codeLayout.fontFamily.toLowerCase()).toMatch(/mono|consolas|menlo/);
  expect(codeLayout.lineDisplays.length).toBeGreaterThan(1);
  expect(codeLayout.lineDisplays.every((display) => display === "block")).toBe(true);
  await expect(page.getByTitle("复制代码")).toBeVisible();
  const thinkingIconMargin = await page.locator(".thinking-trigger > svg").first().evaluate(
    (icon) => getComputedStyle(icon).marginLeft,
  );
  expect(thinkingIconMargin).toBe("0px");
  await expect(page.getByRole("img", { name: /Mermaid/ })).toBeVisible();
  if (page.viewportSize()?.width === 1440) {
    await page.screenshot({ path: ".pytest_artifacts/frontend-desktop.png", fullPage: true });
  }
});

test("展示结构化错误并停止活跃 Turn", async ({ page }) => {
  const input = page.getByPlaceholder("输入消息，或附加文本与图片");
  await input.fill("触发错误");
  await page.getByRole("button", { name: "发送" }).click();
  await expect(page.getByRole("alert")).toContainText("Fake 结构化错误");
  await page.getByRole("button", { name: "关闭错误" }).click();

  await input.fill("等待停止");
  await page.getByRole("button", { name: "发送" }).click();
  await page.getByRole("button", { name: "停止" }).click();
  await expect(page.getByText("已停止")).toBeVisible();
});

test("上传文本附件并在断线后自动重连", async ({ page }) => {
  const chooser = page.locator('input[type="file"]');
  await chooser.setInputFiles([
    { name: "notes.txt", mimeType: "text/plain", buffer: Buffer.from("attachment") },
    { name: "photo.png", mimeType: "image/png", buffer: Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M/wHwAF/gL+X8dJAAAAAElFTkSuQmCC", "base64") },
  ]);
  await expect(page.getByText("notes.txt")).toBeVisible();
  await expect(page.locator(".pending-file img")).toBeVisible();
  await page.getByPlaceholder("输入消息，或附加文本与图片").fill("附件测试");
  await page.getByRole("button", { name: "发送" }).click();
  await expect(page.getByText("notes.txt")).toBeVisible();
  await expect(page.getByAltText("photo.png")).toBeVisible();

  await expect(page.getByRole("button", { name: "重连中" })).toBeVisible();
  await expect(page.getByRole("button", { name: "已连接" })).toBeVisible({ timeout: 5_000 });
});

test("移动端布局没有横向溢出", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "mobile", "仅移动端项目执行布局断言");
  const dimensions = await page.evaluate(() => ({
    viewport: document.documentElement.clientWidth,
    content: document.documentElement.scrollWidth,
  }));
  expect(dimensions.content).toBeLessThanOrEqual(dimensions.viewport);
  await expect(page.getByRole("button", { name: "打开会话列表" })).toBeVisible();
  await page.screenshot({ path: ".pytest_artifacts/frontend-mobile.png", fullPage: true });
});

test("加载历史会话并新建空会话", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "桌面侧栏覆盖即可");
  await expect(page.getByRole("button", { name: "打开会话列表" })).toBeHidden();
  await page.getByRole("button", { name: "历史问题", exact: true }).click();
  await expect(page.getByText("历史回答")).toBeVisible();
  await page.getByRole("button", { name: "新建会话" }).click();
  await expect(page.getByText("从一个具体问题开始")).toBeVisible();
});

test("长回答只滚动消息区并始终保留输入框", async ({ page }) => {
  await page.getByPlaceholder("输入消息，或附加文本与图片").fill("长回答布局测试");
  await page.getByRole("button", { name: "发送" }).click();
  await expect(page.getByText("长回答结束")).toBeVisible();

  const layout = await page.evaluate(() => {
    const conversation = document.querySelector<HTMLElement>(".conversation-scroll");
    const composer = document.querySelector<HTMLElement>(".composer-wrap");
    if (!conversation || !composer) throw new Error("聊天布局节点缺失");
    const composerRect = composer.getBoundingClientRect();
    return {
      composerBottom: composerRect.bottom,
      composerTop: composerRect.top,
      conversationScrollable: conversation.scrollHeight > conversation.clientHeight,
      viewportHeight: window.innerHeight,
    };
  });

  expect(layout.conversationScrollable).toBe(true);
  expect(layout.composerTop).toBeGreaterThan(0);
  expect(layout.composerBottom).toBeLessThanOrEqual(layout.viewportHeight);
  await expect.poll(() => page.locator(".conversation-scroll").evaluate((element) => (
    element.scrollTop + element.clientHeight >= element.scrollHeight - 2
  ))).toBe(true);
  await page.locator(".conversation-scroll").evaluate((element) => {
    element.scrollTop = element.scrollHeight;
  });
});
