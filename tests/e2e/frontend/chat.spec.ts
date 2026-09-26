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

  await ensureToolGroupExpanded(page, /已工作/);
  await expect(page.getByText("list_dir")).toBeVisible();
  await expect(page.getByRole("button", { name: /list_dir.*完成/ })).toBeVisible();
  await expect(page.getByText("最终内容", { exact: true })).toBeVisible();
  await expect(page.getByText("流式草稿", { exact: true })).toHaveCount(0);
  await expect(page.locator("pre code")).toContainText("print");
  await page.getByRole("button", { name: /思考过程/ }).click();
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
  const thinkingIconMargin = await page.locator(".process-row > svg").first().evaluate(
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

  const userMessage = page.locator(".user-message").filter({ hasText: "附件测试" });
  await expect(userMessage.locator(".attachment-gallery")).toHaveCSS("justify-content", "flex-end");
  // 比较真实布局边界，覆盖桌面横排与移动端换行，避免只检查类名而漏掉样式回归。
  const alignment = await userMessage.evaluate((message) => {
    const image = message.querySelector(".image-attachment")!.getBoundingClientRect();
    const text = message.querySelector(".user-text")!.getBoundingClientRect();
    const body = message.querySelector(".message-body")!.getBoundingClientRect();
    return {
      rightDifference: Math.abs(image.right - text.right),
      staysInside: image.left >= body.left - 1 && image.right <= body.right + 1,
    };
  });
  expect(alignment.rightDifference).toBeLessThanOrEqual(1);
  expect(alignment.staysInside).toBe(true);

  await expect(page.getByRole("button", { name: "重连中" })).toBeVisible();
  await expect(page.getByRole("button", { name: "已连接" })).toBeVisible({ timeout: 5_000 });
  await page.getByRole("button", { name: "深色", exact: true }).click();
  await expect(userMessage.locator(".message-body")).toHaveCSS("background-color", "rgba(0, 0, 0, 0)");
  await expect(userMessage.locator(".attachment-gallery")).toHaveCSS("justify-content", "flex-end");
});

test("深色模式用户消息只有气泡着色且切换刷新不出现整行背景", async ({ page }, testInfo) => {
  await page.route("**/api/chat/sessions/*/messages**", async (route) => {
    await route.fulfill({ json: {
      session_id: "web:history", total: 2, items: [
        { id: "theme-user", role: "user", content: "2", turn_id: "theme-turn" },
        { id: "theme-answer", role: "assistant", content: "收到，消息显示正常。", turn_id: "theme-turn" },
      ],
    } });
  });
  if (testInfo.project.name === "mobile") await page.getByRole("button", { name: "打开会话列表" }).click();
  await page.getByRole("navigation", { name: "会话列表" }).getByRole("button", { name: "历史问题", exact: true }).click();
  const user = page.locator(".user-message");
  const body = user.locator(".message-body");
  const bubble = user.locator(".user-text");
  await expect(bubble).toHaveText("2");
  for (const theme of ["浅色", "深色", "浅色", "深色"]) {
    await page.getByRole("button", { name: theme, exact: true }).click();
    await expect(body).toHaveCSS("background-color", "rgba(0, 0, 0, 0)");
    await expect(user).toHaveCSS("background-color", "rgba(0, 0, 0, 0)");
    await expect(bubble).not.toHaveCSS("background-color", "rgba(0, 0, 0, 0)");
    await expect(bubble).toHaveCSS("border-radius", "20px");
  }
  // 容器继续承担阅读宽度和右对齐，不通过缩窄容器掩盖错误背景。
  const geometry = await body.evaluate((element) => {
    const outer = element.getBoundingClientRect();
    const inner = element.querySelector(".user-text")!.getBoundingClientRect();
    return { rightGap: Math.abs(outer.right - inner.right), bubbleWidth: inner.width, bodyWidth: outer.width };
  });
  expect(geometry.rightGap).toBeLessThanOrEqual(1);
  expect(geometry.bubbleWidth).toBeLessThan(geometry.bodyWidth / 2);
  await page.reload();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await expect(bubble).toHaveText("2");
  await expect(body).toHaveCSS("background-color", "rgba(0, 0, 0, 0)");
  await page.screenshot({ path: `.pytest_artifacts/user-message-dark-${testInfo.project.name}.png`, animations: "disabled" });
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

test("品牌 SVG 在网站图标与新对话页正确加载且保留原尺寸", async ({ page }, testInfo) => {
  const mobile = testInfo.project.name === "mobile";
  const welcomeLogo = page.locator(".empty-mark");
  const headerLogo = page.locator(mobile ? ".brand-compact .brand-mark" : ".brand-lockup .brand-mark");
  await expect(welcomeLogo).toBeVisible();
  await expect(welcomeLogo).toHaveCSS("width", "42px");
  await expect(welcomeLogo).toHaveCSS("height", "42px");
  await expect(headerLogo).toBeVisible();
  await expect(headerLogo).toHaveCSS("width", mobile ? "30px" : "32px");
  await expect(headerLogo).toHaveCSS("height", mobile ? "30px" : "32px");
  await expect(welcomeLogo).toHaveCSS("box-shadow", "none");
  const mask = await welcomeLogo.evaluate((element) => getComputedStyle(element).maskImage);
  expect(mask).toContain("svg");
  const favicon = page.locator('link[rel="icon"]');
  await expect(favicon).toHaveAttribute("type", "image/svg+xml");
  // 验证构建后的资源确实可解码，避免只替换入口却遗漏静态文件。
  const faviconLoaded = await favicon.evaluate((element) => new Promise<boolean>((resolve) => {
    const image = new Image();
    image.onload = () => resolve(image.naturalWidth > 0 && image.naturalHeight > 0);
    image.onerror = () => resolve(false);
    image.src = (element as HTMLLinkElement).href;
  }));
  expect(faviconLoaded).toBe(true);
  await page.getByRole("button", { name: "浅色", exact: true }).click();
  await expect(welcomeLogo).toHaveCSS("background-color", "rgb(11, 118, 110)");
  await page.screenshot({ path: `.pytest_artifacts/beanagent-logo-light-${testInfo.project.name}.png` });
  await page.getByRole("button", { name: "深色", exact: true }).click();
  await expect(welcomeLogo).toHaveCSS("background-color", "rgb(98, 184, 170)");
  await page.screenshot({ path: `.pytest_artifacts/beanagent-logo-dark-${testInfo.project.name}.png` });
  if (mobile) {
    await page.getByRole("button", { name: "打开会话列表" }).click();
    await expect(page.locator(".brand-lockup .brand-mark:visible")).toHaveCSS("width", "32px");
  }
});

test("消息阅读排版、过程键盘折叠与长代码复制在窄屏仍可用", async ({ page }, testInfo) => {
  const source = "const message = '" + "正文与代码分别控制滚动范围".repeat(14) + "';";
  const content = [
    "## 聊天界面调整", "", "用户消息与附件靠右，助手正文保持清晰的阅读层次。", "",
    "- 正文平铺，减少边框", "- 工具过程可以展开", "",
    "```ts", source, "```", "", "未闭合的代码围栏仍可显示：", "", "```text", "仍在输出",
  ].join("\n");
  await page.route("**/api/chat/sessions/*/messages**", async (route) => {
    await route.fulfill({ json: {
      session_id: "web:history", total: 2, items: [
        { id: "reading-user", role: "user", content: "查看聊天区的排版效果", turn_id: "reading-turn" },
        { id: "reading-answer", role: "assistant", content, turn_id: "reading-turn",
          reasoning_content: "先检查原有消息结构，再调整展示。",
          tool_chain: [{ calls: [{ call_id: "reading-tool", name: "read_file", status: "completed", result: "已读取" }] }] },
      ],
    } });
  });
  if (testInfo.project.name === "mobile") await page.getByRole("button", { name: "打开会话列表" }).click();
  await page.getByRole("button", { name: "历史问题", exact: true }).click();
  await ensureToolGroupExpanded(page, /已工作/);
  const summary = page.getByRole("button", { name: /思考过程/ });
  const tools = page.getByRole("button", { name: /read_file.*完成/ });
  await expect(tools).toHaveAttribute("aria-expanded", "false");
  await expect(summary).toHaveAttribute("aria-expanded", "false");
  await summary.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByText("先检查原有消息结构，再调整展示。")).toBeVisible();
  await expect(tools).toHaveAttribute("aria-expanded", "false");
  await page.keyboard.press("Space");
  await expect(summary).toHaveAttribute("aria-expanded", "false");
  await expect(page.locator('[data-streamdown="code-block"]')).toHaveCount(2);
  await page.evaluate(() => {
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: {
      writeText: async (text: string) => { document.documentElement.dataset.copiedCode = text; },
    } });
  });
  await page.getByTitle("复制代码").first().click();
  await expect(page.locator("html")).toHaveAttribute("data-copied-code", source + "\n");
  const geometry = await page.locator('[data-streamdown="code-block"]').first().evaluate((block) => {
    const bounds = block.getBoundingClientRect();
    const copy = block.querySelector('[data-streamdown="code-block-copy-button"]')!.getBoundingClientRect();
    const header = block.querySelector('[data-streamdown="code-block-header"]')!.getBoundingClientRect();
    const body = block.querySelector('[data-streamdown="code-block-body"]')!;
    return {
      viewport: document.documentElement.clientWidth, pageWidth: document.documentElement.scrollWidth,
      blockWidth: bounds.width, copyRightGap: bounds.right - copy.right,
      copyInsideHeader: copy.top >= header.top && copy.bottom <= header.bottom,
      codeScrollable: body.scrollWidth > body.clientWidth,
      readingWidth: block.closest(".message")!.getBoundingClientRect().width,
      lineHeight: getComputedStyle(block.closest(".message-body")!).lineHeight,
    };
  });
  expect(geometry.pageWidth).toBeLessThanOrEqual(geometry.viewport);
  expect(geometry.readingWidth).toBeLessThanOrEqual(720);
  expect(geometry.lineHeight).toBe("27px");
  expect(geometry.copyInsideHeader).toBe(true);
  expect(geometry.copyRightGap).toBeLessThanOrEqual(16);
  expect(geometry.copyRightGap).toBeGreaterThanOrEqual(0);
  expect(geometry.codeScrollable).toBe(true);
  await page.locator(".conversation-scroll").evaluate((element) => { element.scrollTop = 0; });
  await page.screenshot({ path: `.pytest_artifacts/chat-reading-${testInfo.project.name}.png` });
  await page.getByRole("button", { name: "深色", exact: true }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await page.screenshot({ path: `.pytest_artifacts/chat-reading-dark-${testInfo.project.name}.png` });
});

test("加载历史会话并新建空会话", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "桌面侧栏覆盖即可");
  await expect(page.getByRole("button", { name: "打开会话列表" })).toBeHidden();
  await page.getByRole("button", { name: "历史问题", exact: true }).click();
  await expect(page.getByText("历史回答")).toBeVisible();
  await page.getByRole("button", { name: "新对话", exact: true }).click();
  await expect(page.getByText("今天想让 BeanAgent 帮你做什么？")).toBeVisible();
});

test("工作目录与会话权限可以在输入框切换", async ({ page }) => {
  await page.getByRole("button", { name: "工作目录：无工作目录" }).click();
  await page.getByRole("menuitemradio", { name: /Bean Demo/ }).click();
  await page.getByRole("button", { name: "权限：只读" }).click();
  await page.getByRole("menuitemradio", { name: /工作区可写/ }).click();

  await expect(page.getByRole("button", { name: "工作目录：Bean Demo" })).toBeVisible();
  await expect(page.getByRole("button", { name: "权限：工作区可写" })).toBeVisible();
});

test("侧栏分组和目录独立折叠且不关闭当前会话", async ({ page }, testInfo) => {
  const mobile = testInfo.project.name === "mobile";
  const historySession = page.getByRole("navigation", { name: "会话列表" })
    .getByRole("button", { name: "历史问题", exact: true });
  if (mobile) await page.getByRole("button", { name: "打开会话列表" }).click();
  await historySession.click();
  await expect(page.getByText("历史回答")).toBeVisible();
  if (mobile) await page.getByRole("button", { name: "打开会话列表" }).click();
  const directory = page.getByRole("button", { name: "工作目录“Bean Demo”的会话", exact: true });
  const projects = page.getByRole("button", { name: "项目", exact: true });
  await directory.click();
  await expect(directory).toHaveAttribute("aria-expanded", "false");
  await expect(historySession).toBeHidden();
  await projects.focus();
  await page.keyboard.press("Enter");
  await expect(directory).toBeHidden();
  await page.keyboard.press("Space");
  await expect(directory).toBeVisible();
  await expect(directory).toHaveAttribute("aria-expanded", "false");
  await page.getByRole("button", { name: "打开工作目录“Bean Demo”的菜单" }).click();
  await expect(page.getByRole("menuitem", { name: "修改名称" })).toBeVisible();
  await expect(directory).toHaveAttribute("aria-expanded", "false");
  await directory.click();
  await expect(historySession).toBeVisible();
  await expect(page.getByText("历史回答")).toBeVisible();
  await expect(page).toHaveURL(/\/chat\/history$/);
  const recent = page.getByRole("button", { name: "最近", exact: true });
  await recent.click();
  await expect(recent).toHaveAttribute("aria-expanded", "false");
  await page.screenshot({ path: `.pytest_artifacts/sidebar-fold-${testInfo.project.name}.png` });
  await page.getByRole("button", { name: "在最近中新建会话" }).click();
  if (mobile) await page.getByRole("button", { name: "打开会话列表" }).click();
  await expect(recent).toHaveAttribute("aria-expanded", "true");
});

test("侧栏折叠刷新后保留并在收起时显示数量", async ({ page }, testInfo) => {
  const mobile = testInfo.project.name === "mobile";
  if (mobile) await page.getByRole("button", { name: "打开会话列表" }).click();
  const directory = page.getByRole("button", { name: "工作目录“Bean Demo”的会话", exact: true });
  const projects = page.getByRole("button", { name: "项目", exact: true });
  const recent = page.getByRole("button", { name: "最近", exact: true });
  await directory.click();
  await expect(directory).toHaveAccessibleDescription("1 个会话");
  await recent.click();
  await projects.click();
  await expect(projects).toHaveAccessibleDescription("1 个目录");
  await page.reload();
  await expect(page.getByRole("button", { name: "已连接" })).toBeVisible();
  if (mobile) await page.getByRole("button", { name: "打开会话列表" }).click();
  await expect(projects).toHaveAttribute("aria-expanded", "false");
  await expect(recent).toHaveAttribute("aria-expanded", "false");
  await expect(projects).toHaveAccessibleDescription("1 个目录");
  await projects.click();
  await expect(directory).toHaveAttribute("aria-expanded", "false");
  await expect(directory.getByText("1 个会话", { exact: true })).toBeVisible();
  await expect(projects.locator(".sidebar-fold-count")).toHaveCount(0);
  await page.screenshot({ path: `.pytest_artifacts/sidebar-fold-persist-${testInfo.project.name}.png` });
  await directory.click();
  await expect(directory.locator(".sidebar-fold-count")).toHaveCount(0);
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
  await ensureToolGroupExpanded(page, /已工作/);
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
  // 完成后过程默认收起；失败结果也保留在各自工具行内。
  await ensureToolGroupExpanded(page, /已工作/);
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
