import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import type { ComponentProps } from "react";
import { afterEach, expect, it, vi } from "vitest";

import { SessionSidebar } from "./SessionSidebar";

type SidebarProps = ComponentProps<typeof SessionSidebar>;

function renderSidebar(overrides: Partial<SidebarProps> = {}) {
  const props: SidebarProps = {
    sessions: [],
    workspaces: [],
    activeSessionId: "",
    onCreate: vi.fn(),
    onDelete: vi.fn(async () => undefined),
    onDeleteWorkspace: vi.fn(async () => undefined),
    onOpenWorkspace: vi.fn(async () => undefined),
    onRegisterWorkspace: vi.fn(async () => ({
      id: "workspace-1",
      canonical_path: "D:/code/demo",
      title: "Demo",
      created_at: "2026-09-24T00:00:00Z",
      updated_at: "2026-09-24T00:00:00Z",
      valid: true,
    })),
    onRename: vi.fn(async () => undefined),
    onSetSessionPinned: vi.fn(async () => undefined),
    onUpdateWorkspace: vi.fn(async () => undefined),
    onSelect: vi.fn(),
    onSettings: vi.fn(),
    onExtension: vi.fn(),
    ...overrides,
  };
  return { ...render(<SessionSidebar {...props} />), props };
}

afterEach(() => cleanup());

it("快捷入口默认中性且点击后保持互斥选中", () => {
  const onCreate = vi.fn();
  renderSidebar({ onCreate });
  const createButton = screen.getByRole("button", { name: "新对话" });
  const workspaceButton = screen.getByRole("button", { name: "添加工作目录" });

  expect(createButton).toHaveAttribute("aria-pressed", "false");
  expect(workspaceButton).toHaveAttribute("aria-pressed", "false");
  fireEvent.click(createButton);
  expect(createButton).toHaveAttribute("aria-pressed", "true");
  expect(workspaceButton).toHaveAttribute("aria-pressed", "false");
  expect(onCreate).toHaveBeenCalledWith(null);

  fireEvent.click(workspaceButton);
  expect(createButton).toHaveAttribute("aria-pressed", "false");
  expect(workspaceButton).toHaveAttribute("aria-pressed", "true");
});

it("扩展父栏可收起并按路由标记当前子项", () => {
  const onExtension = vi.fn();
  const { rerender, props } = renderSidebar({
    activeExtension: null,
    extensionCounts: { plugins: 2, mcp: 3, skills: 6 },
    onExtension,
  });
  const extensionToggle = screen.getByRole("button", { name: /扩展/ });

  expect(extensionToggle).toHaveAttribute("aria-expanded", "true");
  expect(screen.getByRole("button", { name: "插件，2 项" })).not.toHaveAttribute("aria-current");
  fireEvent.click(screen.getByRole("button", { name: "MCP，3 项" }));
  expect(onExtension).toHaveBeenCalledWith("mcp");

  rerender(<SessionSidebar {...props} activeExtension="mcp" />);
  expect(screen.getByRole("button", { name: "MCP，3 项" })).toHaveAttribute("aria-current", "page");
  expect(screen.getByRole("button", { name: "MCP，3 项" })).toHaveClass("active");

  fireEvent.click(extensionToggle);
  expect(extensionToggle).toHaveAttribute("aria-expanded", "false");
  expect(screen.queryByRole("button", { name: "插件，2 项" })).not.toBeInTheDocument();
});
