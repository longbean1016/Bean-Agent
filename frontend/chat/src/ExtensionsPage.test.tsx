import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import { ExtensionsPage } from "./ExtensionsPage";

const weatherRecord = {
  id: "user:weather",
  name: "weather",
  description: "天气查询",
  source: "user",
  scope: "user",
  available: true,
  enabled: true,
  always: false,
  missing: "",
  status: "available",
  revision: 1,
};

function jsonResponse(payload: unknown, ok = true): Response {
  return { ok, json: async () => payload } as Response;
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

it("展示默认 Skill 状态、差异并在确认后恢复和刷新", async () => {
  let status = "user_modified";
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.startsWith("/api/extensions/skills/revision")) return jsonResponse({ revision: "r1" });
    if (url === "/api/extensions/skills?scope=workspace") return jsonResponse({ items: [] });
    if (url === "/api/extensions/skills?scope=user") return jsonResponse({ items: [weatherRecord] });
    if (url === "/api/extensions/skills/defaults") return jsonResponse({ items: [{ name: "weather", status, user_hash: "user-hash", default_hash: "default-hash", installed_hash: "old-hash", managed: false }] });
    if (url === "/api/extensions/skills/defaults/weather/diff") return jsonResponse({ name: "weather", status, user_hash: "user-hash", default_hash: "default-hash", installed_hash: "old-hash", managed: false, diff: "-用户正文\n+默认正文\n" });
    if (url === "/api/extensions/skills/defaults/weather/restore" && init?.method === "POST") {
      status = "current";
      return jsonResponse({ item: { name: "weather", status, user_hash: "default-hash", default_hash: "default-hash", installed_hash: "default-hash", managed: true } });
    }
    throw new Error(`未处理请求: ${url}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);

  render(<ExtensionsPage kind="skills" onBack={() => undefined} />);
  fireEvent.click(screen.getByRole("button", { name: "用户级" }));

  expect(await screen.findByText("已由用户修改")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "更新默认版本" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "查看差异" }));
  const dialog = await screen.findByRole("dialog", { name: "weather 默认版本差异" });
  expect(within(dialog).getByText(/用户正文/)).toBeInTheDocument();
  fireEvent.click(within(dialog).getByRole("button", { name: "关闭差异" }));

  fireEvent.click(screen.getByRole("button", { name: "恢复默认" }));
  expect(confirm).toHaveBeenCalledWith(expect.stringContaining("用户修改会被覆盖"));
  expect(await screen.findByRole("status")).toHaveTextContent("已恢复默认 Skill「weather」");
  expect(await screen.findByText("默认版本已是最新")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "恢复默认" })).toBeDisabled();
  const restoreCall = fetchMock.mock.calls.find(([url]) => String(url).endsWith("/restore"));
  expect(restoreCall?.[1]?.body).toBe(JSON.stringify({ expected_hash: "user-hash" }));
});

it("默认 Skill 更新失败时保留列表并展示服务端原因", async () => {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.startsWith("/api/extensions/skills/revision")) return jsonResponse({ revision: "r1" });
    if (url === "/api/extensions/skills?scope=workspace") return jsonResponse({ items: [] });
    if (url === "/api/extensions/skills?scope=user") return jsonResponse({ items: [weatherRecord] });
    if (url === "/api/extensions/skills/defaults") return jsonResponse({ items: [{ name: "weather", status: "update_available", user_hash: "old-hash", default_hash: "new-hash", installed_hash: "old-hash", managed: true }] });
    if (url === "/api/extensions/skills/defaults/weather/update" && init?.method === "POST") return jsonResponse({ detail: { code: "seed_conflict", detail: "Skill 文件已变化，请刷新后重试", retryable: true } }, false);
    throw new Error(`未处理请求: ${url}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  vi.spyOn(window, "confirm").mockReturnValue(true);

  render(<ExtensionsPage kind="skills" onBack={() => undefined} />);
  fireEvent.click(screen.getByRole("button", { name: "用户级" }));
  expect(await screen.findByText("有默认版本更新")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "更新默认版本" }));

  await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("Skill 文件已变化，请刷新后重试"));
  expect(screen.getByText("有默认版本更新")).toBeInTheDocument();
});

it("查看 Skill 详情不会打开可保存编辑器", async () => {
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.startsWith("/api/extensions/skills/revision")) return jsonResponse({ revision: "r1" });
    if (url === "/api/extensions/skills?scope=workspace") return jsonResponse({ items: [weatherRecord] });
    if (url === "/api/extensions/skills/weather?scope=workspace") return jsonResponse({ ...weatherRecord, content: "天气正文", file_path: "D:/skills/weather/SKILL.md" });
    throw new Error(`未处理请求: ${url}`);
  }));

  render(<ExtensionsPage kind="skills" onBack={() => undefined} />);
  await screen.findByText("天气查询");
  fireEvent.click(screen.getByLabelText("查看技能 weather"));

  expect(await screen.findByText("Skill 详情")).toBeInTheDocument();
  expect(screen.getByText("天气正文")).toBeInTheDocument();
  expect(screen.queryByLabelText("SKILL.md 内容")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "保存" })).not.toBeInTheDocument();
});
