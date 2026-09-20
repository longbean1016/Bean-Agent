import { expect, test, type Page } from "@playwright/test";

const browserErrors = new WeakMap<Page, string[]>();

async function ensureToolGroupExpanded(page: Page, name: RegExp): Promise<void> {
  const trigger = page.getByRole("button", { name });
  await expect(trigger).toBeVisible();
  if (await trigger.getAttribute("aria-expanded") !== "true") await trigger.click();
  await expect(trigger).toHaveAttribute("aria-expanded", "true");
}

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

  await ensureToolGroupExpanded(page, /工具调用 · 1 项完成/);
  await expect(page.getByText("list_dir")).toBeVisible();
  await expect(page.getByRole("button", { name: /list_dir.*完成/ })).toBeVisible();
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
  await page.getByRole("button", { name: "停止", exact: true }).click();
  await expect(page.getByText("已停止")).toBeVisible();
});

test("展示排队位置并允许取消等待任务", async ({ page }) => {
  const input = page.getByPlaceholder("输入消息，或附加文本与图片");
  await input.fill("排队测试");
  await page.getByRole("button", { name: "发送" }).click();

  await expect(page.getByText("排队中 · 即将开始")).toBeVisible();
  await expect(page.getByText("排队中 · 前面还有 1 个会话")).toBeVisible();
  await page.getByRole("button", { name: "停止" }).click();

  await expect(page.getByText("排队中 · 前面还有 1 个会话")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "发送" })).toBeVisible();
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
  await page.getByRole("button", { name: "新建会话", exact: true }).click();
  await expect(page.getByText("从一个具体问题开始")).toBeVisible();
});

test("工作目录与会话权限可以在输入框切换", async ({ page }) => {
  await page.getByRole("button", { name: "工作目录：无工作目录" }).click();
  await page.getByRole("menuitemradio", { name: /Bean Demo/ }).click();
  await page.getByRole("button", { name: "权限：只读" }).click();
  await page.getByRole("menuitemradio", { name: /工作区可写/ }).click();

  await expect(page.getByRole("button", { name: "工作目录：Bean Demo" })).toBeVisible();
  await expect(page.getByRole("button", { name: "权限：工作区可写" })).toBeVisible();
});

test("添加工作目录使用本机选择器返回路径", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "桌面侧栏覆盖即可");
  await page.getByRole("button", { name: "添加工作目录" }).click();
  const dialog = page.getByRole("dialog", { name: "添加工作目录" });
  await expect(dialog.getByLabel("绝对路径")).toHaveCount(0);
  await dialog.getByRole("button", { name: /选择 Bean 可读取和编辑的文件夹/ }).click();
  await expect(dialog.getByText("D:/projects/picked-by-native-dialog")).toBeVisible();
  await expect(dialog.getByRole("button", { name: "添加" })).toBeEnabled();
});

test("完全访问需要显式风险确认", async ({ page }) => {
  await page.getByRole("button", { name: "权限：只读" }).click();
  await page.getByRole("menuitemradio", { name: /完全访问/ }).click();
  const dialog = page.getByRole("dialog", { name: "启用完全访问？" });
  const confirm = dialog.getByRole("button", { name: "启用完全访问" });
  await expect(confirm).toBeDisabled();
  await dialog.getByRole("checkbox").check();
  await confirm.click();
  await expect(page.getByRole("button", { name: "权限：完全访问" })).toBeVisible();
});

test("越权请求在对应工具行内显示审批卡并只允许单次决定", async ({ page }) => {
  const input = page.getByPlaceholder("输入消息，或附加文本与图片");
  await input.fill("审批测试");
  await page.getByRole("button", { name: "发送" }).click();

  await expect(page.getByLabel("待处理权限审批")).toBeVisible();
  await expect(page.getByText("Set-Content D:\\outside.txt '[内容已隐藏]'")).toBeVisible();
  // 审批只绑定当前工具行，不替换 Composer，也不再次展示三档权限菜单。
  await expect(input).toBeVisible();
  await expect(page.getByText("playwright-fingerprint")).toHaveCount(0);
  await page.getByRole("button", { name: "仅允许本次" }).click();
  await expect(page.getByText("审批流程已结束")).toBeVisible();
  await ensureToolGroupExpanded(page, /工具调用 · 1 项完成/);
  await expect(page.getByRole("button", { name: /shell.*完成/ })).toBeVisible();
  await expect(page.getByPlaceholder("输入消息，或附加文本与图片")).toBeVisible();
});

test("拒绝审批后工具进入失败终态并清理等待卡", async ({ page }) => {
  const input = page.getByPlaceholder("输入消息，或附加文本与图片");
  await input.fill("审批测试");
  await page.getByRole("button", { name: "发送" }).click();

  await expect(page.getByLabel("待处理权限审批")).toBeVisible();
  await page.getByRole("button", { name: "拒绝" }).click();
  await expect(page.getByText("审批流程已结束")).toBeVisible();
  // 失败组默认保持可见，但工具详情仍遵循完成组的折叠策略；先展开组再断言单行状态。
  await ensureToolGroupExpanded(page, /工具调用 · 1 项中 1 项失败/);
  await expect(page.getByRole("button", { name: /shell.*已拒绝/ })).toBeVisible();
  await expect(page.getByLabel("待处理权限审批")).toHaveCount(0);
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
