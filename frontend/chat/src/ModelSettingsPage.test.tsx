import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import { ModelSettingsPage } from "./ModelSettingsPage";
import type { ModelSettingsPayload } from "./types";

const settings: ModelSettingsPayload = {
  routing_required: true,
  default_route: { connection_id: "company", model_id: "model-a" },
  catalog: {},
  connections: [{
    id: "company",
    name: "公司 API",
    provider: "",
    base_url: "https://example.com/v1",
    has_api_key: true,
    api_key_preview: "sk-t...5678",
    enabled: true,
    default_adapter: "generic_openai",
    revision: 1,
    created_at: "",
    updated_at: "",
    models: [{
      connection_id: "company",
      model_id: "model-a",
      display_name: "Model A",
      context_window: 128000,
      max_output_tokens: 8192,
      supports_tools: true,
      supports_vision: false,
      supports_reasoning: false,
      reasoning_options: [],
      adapter: "generic_openai",
      metadata_source: "models.dev:test",
      metadata_updated_at: null,
      user_overrides: {},
      available: true,
      revision: 1,
      discovered_at: "",
    }],
  }],
};

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

it("明确显示密钥状态并反馈模型列表和所选模型测试结果", async () => {
  const writeText = vi.fn(async () => undefined);
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: { writeText },
  });
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.endsWith("/api-key")) return {
      ok: true,
      json: async () => ({ api_key: "sk-test-12345678" }),
    } as Response;
    if (url.endsWith("/models/model-a/test")) return {
      ok: true,
      json: async () => ({
        ok: true,
        connection_id: "company",
        connection_name: "公司 API",
        model_id: "model-a",
        model_display_name: "Model A",
        adapter: "generic_openai",
        duration_ms: 36,
      }),
    } as Response;
    return {
      ok: true,
      json: async () => ({
        ok: true,
        connection_id: "company",
        connection_name: "公司 API",
        model_count: 3,
      }),
    } as Response;
  }));

  render(<ModelSettingsPage
    settings={settings}
    onBack={vi.fn()}
    onRefresh={vi.fn(async () => settings)}
    onDefaultRoute={vi.fn()}
  />);

  expect(await screen.findByText("已配置", { selector: ".api-key-state" })).toBeVisible();
  expect(screen.getByRole("heading", { name: "模型连接" })).toBeVisible();
  expect(screen.queryByRole("dialog", { name: "模型连接" })).not.toBeInTheDocument();
  expect(screen.getByText("sk-t...5678")).toBeVisible();
  expect(screen.getByText("上下文 128K")).toBeVisible();

  fireEvent.click(screen.getByRole("button", { name: "明文查看 API Key" }));
  expect(await screen.findByText("sk-test-12345678")).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "复制 API Key" }));
  await waitFor(() => expect(writeText).toHaveBeenCalledWith("sk-test-12345678"));

  fireEvent.click(screen.getByRole("button", { name: "测试模型列表" }));
  expect(await screen.findByText(/模型列表可用，共返回 3 个模型/)).toBeVisible();

  fireEvent.click(screen.getByRole("button", { name: "测试所选模型" }));
  await waitFor(() => expect(screen.getByText(/公司 API \/ Model A.*调用成功/)).toBeVisible());
});

it("新增连接保持独立草稿，不会切回并覆盖已有连接", async () => {
  render(<ModelSettingsPage
    settings={settings}
    onBack={vi.fn()}
    onRefresh={vi.fn(async () => settings)}
    onDefaultRoute={vi.fn()}
  />);

  fireEvent.click(screen.getByRole("button", { name: "新增连接" }));

  const nameInput = screen.getByPlaceholderText("例如：DeepSeek 官方");
  expect(nameInput).toHaveValue("");
  expect(screen.queryByRole("button", { name: "删除" })).not.toBeInTheDocument();
  fireEvent.change(nameInput, { target: { value: "新连接" } });
  await waitFor(() => expect(nameInput).toHaveValue("新连接"));
});

it("按名称或 ID 搜索当前连接已获取的模型", async () => {
  render(<ModelSettingsPage
    settings={settings}
    onBack={vi.fn()}
    onRefresh={vi.fn(async () => settings)}
    onDefaultRoute={vi.fn()}
  />);

  const search = await screen.findByRole("searchbox", { name: "搜索当前连接的模型" });
  expect(screen.getByText("1/1")).toBeVisible();
  fireEvent.change(search, { target: { value: "model-a" } });
  expect(screen.getByRole("button", { name: /Model Amodel-a/ })).toBeVisible();
  fireEvent.change(search, { target: { value: "不存在" } });
  expect(screen.queryByRole("button", { name: /Model Amodel-a/ })).not.toBeInTheDocument();
  expect(screen.getByText("没有匹配的模型。")).toBeVisible();
});

it("删除连接前展示影响范围并要求确认", async () => {
  const fetchMock = vi.fn(async () => ({ ok: true, json: async () => ({}) }) as Response);
  vi.stubGlobal("fetch", fetchMock);
  render(<ModelSettingsPage
    settings={settings}
    onBack={vi.fn()}
    onRefresh={vi.fn(async () => ({ ...settings, connections: [] }))}
    onDefaultRoute={vi.fn()}
  />);

  fireEvent.click(await screen.findByRole("button", { name: "删除" }));
  expect(screen.getByRole("dialog", { name: "删除连接“公司 API”？" })).toBeVisible();
  expect(screen.getByText(/已获取的模型和数据库中的 API Key/)).toBeVisible();
  expect(fetchMock).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "取消" }));
  expect(screen.queryByRole("dialog", { name: "删除连接“公司 API”？" })).not.toBeInTheDocument();

  fireEvent.click(screen.getByRole("button", { name: "删除" }));
  fireEvent.click(screen.getByRole("button", { name: "确认删除" }));
  await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
    "/api/settings/connections/company",
    expect.objectContaining({ method: "DELETE" }),
  ));
});

it("设置默认模型后通知上层同步当前会话路由", async () => {
  const route = { connection_id: "company", model_id: "model-a" };
  vi.stubGlobal("fetch", vi.fn(async () => ({
    ok: true,
    json: async () => ({ route }),
  }) as Response));
  const onDefaultRoute = vi.fn();
  render(<ModelSettingsPage
    settings={{ ...settings, default_route: null }}
    onBack={vi.fn()}
    onRefresh={vi.fn(async () => ({ ...settings, default_route: route }))}
    onDefaultRoute={onDefaultRoute}
  />);

  fireEvent.click(await screen.findByRole("button", { name: "设为默认" }));
  await waitFor(() => expect(onDefaultRoute).toHaveBeenCalledWith(route));
});

it("设为默认后选中对应模型，直到用户手动选择其他模型", async () => {
  const modelB = {
    ...settings.connections[0].models[0],
    model_id: "model-b",
    display_name: "Model B",
  };
  const routeB = { connection_id: "company", model_id: "model-b" };
  const settingsWithTwoModels: ModelSettingsPayload = {
    ...settings,
    connections: [{
      ...settings.connections[0],
      models: [...settings.connections[0].models, modelB],
    }],
  };
  vi.stubGlobal("fetch", vi.fn(async () => ({
    ok: true,
    json: async () => ({ route: routeB }),
  }) as Response));
  render(<ModelSettingsPage
    settings={settingsWithTwoModels}
    onBack={vi.fn()}
    onRefresh={vi.fn(async () => ({ ...settingsWithTwoModels, default_route: routeB }))}
    onDefaultRoute={vi.fn()}
  />);

  const modelARow = (await screen.findByRole("button", { name: /Model Amodel-a/ })).closest<HTMLElement>(".model-profile-row")!;
  const modelBRow = screen.getByRole("button", { name: /Model Bmodel-b/ }).closest<HTMLElement>(".model-profile-row")!;
  expect(modelARow).toHaveClass("selected");
  fireEvent.click(within(modelBRow).getByRole("button", { name: "设为默认" }));
  await waitFor(() => expect(modelBRow).toHaveClass("selected"));

  fireEvent.click(within(modelARow).getByRole("button", { name: /Model Amodel-a/ }));
  expect(modelARow).toHaveClass("selected");
  expect(modelBRow).not.toHaveClass("selected");
});

it("停用连接说明原因并禁止设为默认", async () => {
  const disabledSettings: ModelSettingsPayload = {
    ...settings,
    connections: settings.connections.map((connection) => ({
      ...connection,
      enabled: false,
    })),
  };
  render(<ModelSettingsPage
    settings={disabledSettings}
    onBack={vi.fn()}
    onRefresh={vi.fn(async () => disabledSettings)}
    onDefaultRoute={vi.fn()}
  />);

  expect(await screen.findByText(/连接已停用.*启用并保存后/)).toBeVisible();
  expect(screen.getByRole("button", { name: "默认" })).toBeDisabled();
});

it("允许手动覆盖推理能力并保存统一选项", async () => {
  let savedRequest: RequestInit | undefined;
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    if (String(input).endsWith("/models/model-a")) savedRequest = init;
    return { ok: true, json: async () => ({}) } as Response;
  });
  vi.stubGlobal("fetch", fetchMock);

  render(<ModelSettingsPage
    settings={settings}
    onBack={vi.fn()}
    onRefresh={vi.fn(async () => settings)}
    onDefaultRoute={vi.fn()}
  />);

  fireEvent.click(screen.getByRole("button", { name: /Model Amodel-a/ }));
  fireEvent.click(screen.getByRole("checkbox", { name: "支持推理" }));
  expect(screen.getByRole("checkbox", { name: "none" })).toBeChecked();
  expect(screen.getByRole("checkbox", { name: "enabled" })).toBeChecked();
  fireEvent.click(screen.getByRole("checkbox", { name: "high" }));
  expect(screen.getByRole("checkbox", { name: "enabled" })).not.toBeChecked();
  fireEvent.click(screen.getByRole("button", { name: "保存能力" }));

  await waitFor(() => {
    expect(savedRequest).toBeDefined();
    expect(JSON.parse(String(savedRequest?.body))).toMatchObject({
      supports_reasoning: true,
      reasoning_options: ["none", "high"],
    });
  });
});
