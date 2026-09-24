import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import type { ComponentProps } from "react";
import type { SessionSummary, Workspace } from "./types";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { SessionSidebar } from "./SessionSidebar";
import { SIDEBAR_FOLD_STORAGE_KEY } from "./useSidebarFoldState";

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

beforeEach(() => localStorage.clear());
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

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

function workspace(id: string, pinned = false): Workspace {
  return { id, title: id, canonical_path: `D:/${id}`, valid: true,
    created_at: "2026-09-24T00:00:00Z", updated_at: "2026-09-24T00:00:00Z",
    pinned_at: pinned ? "2026-09-24T00:00:00Z" : null };
}

function session(key: string, workspaceId?: string): SessionSummary {
  return { key, title: key, first_message_content: key, workspace_id: workspaceId,
    created_at: "2026-09-24T00:00:00Z", updated_at: "2026-09-24T00:00:00Z", message_count: 2 };
}

it("分组与目录独立折叠，收起父组再展开保留子目录状态和选中会话", () => {
  const { props } = renderSidebar({
    workspaces: [workspace("demo"), workspace("pinned", true)],
    sessions: [session("目录会话", "demo"), session("置顶会话", "pinned"), session("最近会话")],
    activeSessionId: "目录会话",
  });
  const directory = screen.getByRole("button", { name: "工作目录“demo”的会话" });
  const project = screen.getByRole("button", { name: "项目" });
  expect(directory).toHaveAttribute("aria-expanded", "true");
  fireEvent.click(directory);
  expect(screen.queryByRole("button", { name: "目录会话" })).not.toBeInTheDocument();
  fireEvent.click(project);
  expect(directory).not.toBeVisible();
  fireEvent.click(project);
  expect(directory).toHaveAttribute("aria-expanded", "false");
  fireEvent.click(directory);
  expect(screen.getByRole("button", { name: "目录会话" }).closest(".session-row")).toHaveClass("active");
  expect(props.onSelect).not.toHaveBeenCalled();
  expect(props.onCreate).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "置顶" }));
  expect(screen.queryByRole("button", { name: "置顶会话" })).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "目录会话" })).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "最近" }));
  expect(screen.queryByRole("button", { name: "最近会话" })).not.toBeInTheDocument();
});

it("目录折叠状态按 ID 保留，不受列表刷新、名称和置顶分组变化影响", () => {
  const { props, rerender } = renderSidebar({ workspaces: [workspace("demo")], sessions: [session("问题", "demo")] });
  fireEvent.click(screen.getByRole("button", { name: "工作目录“demo”的会话" }));
  rerender(<SessionSidebar {...props} workspaces={[{ ...workspace("demo", true), title: "新名称" }]} sessions={[...props.sessions]} />);
  expect(screen.getByRole("button", { name: "工作目录“新名称”的会话" })).toHaveAttribute("aria-expanded", "false");
  expect(screen.queryByRole("button", { name: "问题" })).not.toBeInTheDocument();
});

it("新建和菜单独立于折叠，新建时只展开目标目录和分组", () => {
  const { props } = renderSidebar({ workspaces: [workspace("demo")], sessions: [session("问题", "demo")] });
  const directory = screen.getByRole("button", { name: "工作目录“demo”的会话" });
  fireEvent.click(directory);
  fireEvent.click(screen.getByRole("button", { name: "打开工作目录“demo”的菜单" }));
  expect(directory).toHaveAttribute("aria-expanded", "false");
  expect(screen.getByRole("menuitem", { name: "修改名称" })).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "在“demo”中新建会话" }));
  expect(props.onCreate).toHaveBeenCalledWith("demo");
  expect(directory).toHaveAttribute("aria-expanded", "true");
  const recent = screen.getByRole("button", { name: "最近" });
  fireEvent.click(recent);
  fireEvent.click(screen.getByRole("button", { name: "在最近中新建会话" }));
  expect(props.onCreate).toHaveBeenLastCalledWith(null);
  expect(recent).toHaveAttribute("aria-expanded", "true");
});

it("不可用目录可以收起，目录不可用不会改变历史会话归属", () => {
  renderSidebar({ sessions: [{ ...session("失效目录会话", "missing"), workspace_title: "旧目录", workspace_path: "D:/missing" }] });
  const toggle = screen.getByRole("button", { name: "工作目录“旧目录”的会话" });
  const panel = document.getElementById(toggle.getAttribute("aria-controls")!);
  expect(panel).toBeVisible();
  fireEvent.click(toggle);
  expect(panel).not.toBeVisible();
  fireEvent.click(toggle);
  expect(screen.getByRole("button", { name: "失效目录会话" })).toBeVisible();
});

it("重新挂载恢复各级折叠，数据延迟加载与目录改名不丢失状态", () => {
  const { unmount, props } = renderSidebar({ workspaces: [workspace("demo"), workspace("pin", true)] });
  for (const name of ["工作目录“demo”的会话", "项目", "置顶", "最近"]) {
    fireEvent.click(screen.getByRole("button", { name }));
  }
  expect(JSON.parse(localStorage.getItem(SIDEBAR_FOLD_STORAGE_KEY)!)).toEqual([
    "workspace:demo", "projects", "pinned", "recent",
  ]);
  unmount();
  const restored = renderSidebar();
  restored.rerender(<SessionSidebar {...props} workspaces={[{ ...workspace("demo"), title: "改名目录" }, workspace("pin", true)]} />);
  for (const name of ["项目", "置顶", "最近"]) {
    expect(screen.getByRole("button", { name })).toHaveAttribute("aria-expanded", "false");
  }
  fireEvent.click(screen.getByRole("button", { name: "项目" }));
  expect(screen.getByRole("button", { name: "工作目录“改名目录”的会话" })).toHaveAttribute("aria-expanded", "false");
  fireEvent.click(screen.getByRole("button", { name: "新对话" }));
  expect(JSON.parse(localStorage.getItem(SIDEBAR_FOLD_STORAGE_KEY)!)).not.toContain("recent");
});

it("只在收起时显示对应目录数和会话数，并随列表增删更新", () => {
  const { props, rerender } = renderSidebar({
    workspaces: [workspace("demo"), workspace("pin", true)],
    sessions: [session("问题一", "demo"), session("问题二", "demo"), session("旧问题", "missing"), session("最近问题")],
  });
  expect(document.querySelector(".sidebar-fold-count")).toBeNull();
  const directory = screen.getByRole("button", { name: "工作目录“demo”的会话" });
  fireEvent.click(directory);
  expect(directory).toHaveAccessibleDescription("2 个会话");
  rerender(<SessionSidebar {...props} sessions={[session("仅剩问题", "demo"), session("旧问题", "missing")]} />);
  expect(directory).toHaveAccessibleDescription("1 个会话");
  fireEvent.click(directory);
  expect(directory.querySelector(".sidebar-fold-count")).toBeNull();
  const missing = screen.getByRole("button", { name: "工作目录“不可用工作目录”的会话" });
  fireEvent.click(missing);
  expect(missing).toHaveAccessibleDescription("1 个会话");
  for (const [name, summary] of [["项目", "2 个目录"], ["置顶", "1 个目录"], ["最近", "0 个会话"]]) {
    const toggle = screen.getByRole("button", { name });
    fireEvent.click(toggle);
    expect(toggle).toHaveAccessibleDescription(summary);
  }
});

it.each(["{broken", "null", "{}", "42"])("无效本地数据 %s 回退为展开", (saved) => {
  localStorage.setItem(SIDEBAR_FOLD_STORAGE_KEY, saved);
  renderSidebar();
  expect(screen.getByRole("button", { name: "最近" })).toHaveAttribute("aria-expanded", "true");
});

it("过滤无效折叠键但保留未加载目录的稳定 ID", () => {
  localStorage.setItem(SIDEBAR_FOLD_STORAGE_KEY, JSON.stringify([null, 3, {}, "unknown", "workspace:", "recent", "workspace:later"]));
  renderSidebar();
  expect(screen.getByRole("button", { name: "最近" })).toHaveAttribute("aria-expanded", "false");
  expect(JSON.parse(localStorage.getItem(SIDEBAR_FOLD_STORAGE_KEY)!)).toEqual(["recent", "workspace:later"]);
});

it("浏览器拒绝读写本地存储时仍能操作折叠", () => {
  vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("不可读取"); });
  vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("不可写入"); });
  renderSidebar();
  const recent = screen.getByRole("button", { name: "最近" });
  fireEvent.click(recent);
  expect(recent).toHaveAttribute("aria-expanded", "false");
  fireEvent.click(recent);
  expect(recent).toHaveAttribute("aria-expanded", "true");
});
