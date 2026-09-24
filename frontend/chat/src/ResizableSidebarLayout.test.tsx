import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it } from "vitest";

import { ResizableSidebarLayout } from "./ResizableSidebarLayout";

beforeEach(() => window.localStorage.clear());
afterEach(cleanup);

function renderLayout() {
  return render(<ResizableSidebarLayout sidebar={<nav aria-label="测试侧栏">侧栏内容</nav>}><main>会话页面</main></ResizableSidebarLayout>);
}

it("支持收起和重新展开侧栏且不卸载聊天页面", () => {
  const { container } = renderLayout();
  const layout = container.querySelector(".resizable-sidebar-layout");

  fireEvent.click(screen.getByRole("button", { name: "收起左侧栏" }));
  expect(layout).toHaveClass("sidebar-collapsed");
  expect(screen.getByRole("main")).toHaveTextContent("会话页面");
  expect(screen.getByRole("button", { name: "展开左侧栏" })).toBeVisible();

  fireEvent.click(screen.getByRole("button", { name: "展开左侧栏" }));
  expect(layout).not.toHaveClass("sidebar-collapsed");
  expect(screen.getByRole("navigation", { name: "测试侧栏" })).toBeVisible();
});

it("支持键盘调整宽度并限制在允许范围内", () => {
  renderLayout();
  const resizer = screen.getByRole("separator", { name: /调整左侧栏宽度/ });

  expect(resizer).toHaveAttribute("aria-valuenow", "272");
  fireEvent.keyDown(resizer, { key: "ArrowRight" });
  expect(resizer).toHaveAttribute("aria-valuenow", "280");
  fireEvent.keyDown(resizer, { key: "End" });
  expect(resizer).toHaveAttribute("aria-valuenow", "420");
  fireEvent.keyDown(resizer, { key: "ArrowRight" });
  expect(resizer).toHaveAttribute("aria-valuenow", "420");
  fireEvent.doubleClick(resizer);
  expect(resizer).toHaveAttribute("aria-valuenow", "272");
});

it("拖动分隔线时同步更新侧栏宽度", () => {
  renderLayout();
  const resizer = screen.getByRole("separator", { name: /调整左侧栏宽度/ });

  fireEvent.pointerDown(resizer, { pointerId: 1, clientX: 272 });
  fireEvent.pointerMove(resizer, { pointerId: 1, clientX: 312 });
  fireEvent.pointerUp(resizer, { pointerId: 1, clientX: 312 });

  expect(resizer).toHaveAttribute("aria-valuenow", "312");
});
