import * as Dialog from "@radix-ui/react-dialog";
import { ArrowLeft, Bot, BrainCircuit, Check, CircleCheck, Database, Eye, EyeOff, Image, KeyRound, ListChecks, Play, Plus, RefreshCw, Search, Settings, Trash2, TriangleAlert } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import {
  createModelConnection,
  deleteModelConnection,
  fetchModelConnectionApiKey,
  refreshConnectionModels,
  saveCapabilityRoute,
  saveDefaultModelRoute,
  testCapability,
  testModelConnection,
  testConnectionModel,
  updateModelCatalog,
  updateModelConnection,
  updateModelProfile,
} from "./api";
import type { CapabilityRouteMode, ModelAdapterId, ModelCapability, ModelConnection, ModelProfile, ModelRoute, ModelSettingsPayload } from "./types";
import { REASONING_CHOICES, reasoningStatusForModel, updateReasoningOptions } from "./reasoning";

const ADAPTERS: Array<{ id: ModelAdapterId; label: string }> = [
  { id: "generic_openai", label: "通用 OpenAI" },
  { id: "deepseek", label: "DeepSeek" },
  { id: "qwen_dashscope", label: "Qwen / DashScope" },
  { id: "openai_reasoning", label: "OpenAI Reasoning" },
];

type ConnectionDraft = {
  name: string; provider: string; base_url: string; api_key: string;
  enabled: boolean; default_adapter: ModelAdapterId;
};

type SettingsToast = {
  kind: "success" | "error";
  message: string;
};

const EMPTY_DRAFT: ConnectionDraft = {
  name: "", provider: "", base_url: "", api_key: "", enabled: true,
  default_adapter: "generic_openai",
};

type ConfigurableCapability = Exclude<ModelCapability, "primary">;

const CAPABILITY_TABS: Array<{ id: ModelCapability; label: string; description: string }> = [
  { id: "primary", label: "主模型", description: "对话、推理与工具调用" },
  { id: "embedding", label: "Embedding 模型", description: "记忆检索与向量化" },
  { id: "vision", label: "视觉模型", description: "图片理解与识别" },
];

function capabilityMode(state: { mode?: CapabilityRouteMode | string | null; follows_primary?: boolean } | null | undefined): "follow" | "independent" {
  if (state?.follows_primary === false || state?.mode === "independent") return "independent";
  return "follow";
}

function capabilityIcon(capability: ModelCapability) {
  if (capability === "embedding") return <BrainCircuit size={16} aria-hidden="true" />;
  if (capability === "vision") return <Image size={16} aria-hidden="true" />;
  return <Bot size={16} aria-hidden="true" />;
}

function maskedApiKeyPreview(value: string | null | undefined): string {
  const normalized = String(value || "").trim();
  if (!normalized) return "已配置（仅显示掩码）";
  // 只接受固定的首尾掩码格式；意外返回的完整值即使包含省略号也要重新截断。
  if (/^[^.…*]{1,4}(?:\.{3}|…|•+|\*+)[^.…*]{1,4}$/.test(normalized)) return normalized;
  if (normalized.length <= 7) return "••••••••";
  return `${normalized.slice(0, 3)}…${normalized.slice(-4)}`;
}

export function ModelSettingsPage(props: {
  settings: ModelSettingsPayload;
  onBack: () => void;
  onRefresh: () => Promise<ModelSettingsPayload>;
  onDefaultRoute: (route: ModelRoute) => void;
}) {
  const [activeCapability, setActiveCapability] = useState<ModelCapability>("primary");
  const [capabilityModeOverrides, setCapabilityModeOverrides] = useState<Partial<Record<ConfigurableCapability, "follow" | "independent">>>({});
  const [selectedId, setSelectedId] = useState("");
  const [creating, setCreating] = useState(false);
  const [draft, setDraft] = useState<ConnectionDraft>(EMPTY_DRAFT);
  const [modelQuery, setModelQuery] = useState("");
  const [editingModel, setEditingModel] = useState<ModelProfile | null>(null);
  const [testModelId, setTestModelId] = useState("");
  const [contextDraft, setContextDraft] = useState("");
  const [busy, setBusy] = useState("");
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [toast, setToast] = useState<SettingsToast | null>(null);
  const [revealedApiKey, setRevealedApiKey] = useState<{ connectionId: string; value: string } | null>(null);
  const [deleteConfirmationOpen, setDeleteConfirmationOpen] = useState(false);
  const selectionInitialized = useRef(false);
  const selected = props.settings.connections.find((item) => item.id === selectedId) ?? null;

  const capabilityState = (capability: ConfigurableCapability) => (
    props.settings.capability_routes?.[capability]
    ?? props.settings.capabilities?.[capability]
    ?? null
  );
  const activeCapabilityState = activeCapability === "primary" ? null : capabilityState(activeCapability);
  const activeMode: "follow" | "independent" = activeCapability === "primary"
    ? "independent"
    : (capabilityModeOverrides[activeCapability] ?? capabilityMode(activeCapabilityState));
  const effectiveCapabilityRoute = activeCapability === "primary"
    ? props.settings.default_route
    : (activeCapabilityState?.route
      ?? activeCapabilityState?.override_route
      ?? (activeMode === "follow" ? props.settings.default_route : null));
  const activeCapabilityConnection = effectiveCapabilityRoute
    ? props.settings.connections.find((item) => item.id === effectiveCapabilityRoute.connection_id) ?? null
    : null;
  const connectionEditable = activeCapability === "primary" || activeMode === "independent";
  const configuredIndependentRoute = activeCapabilityState?.route ?? activeCapabilityState?.override_route ?? null;
  const independentRoute = activeCapability === "primary" || activeMode !== "independent" || activeCapabilityState?.mode !== "independent"
    || configuredIndependentRoute?.connection_id === props.settings.default_route?.connection_id
    ? null
    : configuredIndependentRoute;
  const capabilityStateSignature = useMemo(
    () => JSON.stringify(props.settings.capability_routes ?? props.settings.capabilities ?? {}),
    [props.settings.capability_routes, props.settings.capabilities],
  );

  useEffect(() => {
    // 初次设置数据可能异步到达；空列表阶段不要把页面锁在“新增连接”空态。
    if (!props.settings.connections.length) return;
    if (selectionInitialized.current && creating) return;
    if (selectedId && props.settings.connections.some((item) => item.id === selectedId)) return;
    const preferred = effectiveCapabilityRoute?.connection_id
      ? props.settings.connections.find((item) => item.id === effectiveCapabilityRoute?.connection_id)
      : null;
    selectConnection(preferred ?? props.settings.connections[0] ?? null, effectiveCapabilityRoute?.model_id);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [creating, props.settings.connections, selectedId]);

  // 服务端刷新后以持久化路由为准，避免切换会话或其他窗口修改设置后沿用旧草稿状态。
  useEffect(() => {
    setCapabilityModeOverrides({});
  }, [capabilityStateSignature]);

  // 提示只在设置页短暂显示；清理定时器避免切换页面后留下悬挂回调。
  useEffect(() => {
    if (!notice && !error) return;
    setToast({ kind: error ? "error" : "success", message: error || notice });
    const timer = window.setTimeout(() => setToast(null), 4200);
    return () => window.clearTimeout(timer);
  }, [notice, error]);

  const availableModels = useMemo(
    () => selected?.models.filter((model) => model.available) ?? [],
    [selected],
  );
  const filteredModels = useMemo(() => {
    const query = modelQuery.trim().toLocaleLowerCase();
    if (!query) return availableModels;
    return availableModels.filter((model) => (
      model.display_name.toLocaleLowerCase().includes(query)
      || model.model_id.toLocaleLowerCase().includes(query)
    ));
  }, [availableModels, modelQuery]);
  const capabilityRouteForSelection = activeCapability === "primary"
    ? props.settings.default_route
    : (activeCapabilityState?.route ?? activeCapabilityState?.override_route
      ?? (activeMode === "follow" ? props.settings.default_route : null));
  const orderedModels = useMemo(() => {
    const pinnedModelId = capabilityRouteForSelection?.connection_id === selected?.id
      ? capabilityRouteForSelection?.model_id ?? ""
      : "";
    if (!pinnedModelId) return filteredModels;
    return [...filteredModels].sort((left, right) => (
      Number(right.model_id === pinnedModelId) - Number(left.model_id === pinnedModelId)
    ));
  }, [capabilityRouteForSelection?.connection_id, capabilityRouteForSelection?.model_id, filteredModels, selected?.id]);
  const testModel = availableModels.find((model) => model.model_id === testModelId)
    ?? availableModels.find((model) => capabilityRouteForSelection?.connection_id === selected?.id && capabilityRouteForSelection?.model_id === model.model_id)
    ?? availableModels[0];
  const testEffort = testModel?.supports_reasoning
    ? (testModel.capabilities_json?.reasoning?.native?.at(-1) ?? testModel.reasoning_options.at(-1) ?? null)
    : null;

  const currentModelLabel = (capability: ModelCapability): string => {
    const state = capability === "primary" ? null : capabilityState(capability);
    const route = capability === "primary"
      ? props.settings.default_route
      : (state?.route ?? state?.override_route ?? props.settings.default_route);
    if (!route) return "未选择模型";
    const connection = props.settings.connections.find((item) => item.id === route.connection_id);
    return connection?.models.find((model) => model.model_id === route.model_id)?.display_name
      ?? route.model_id
      ?? "未选择模型";
  };

  const selectConnection = (connection: ModelConnection | null, preferredModelId?: string) => {
    selectionInitialized.current = true;
    setCreating(connection === null);
    setSelectedId(connection?.id ?? "");
    setDraft(connection ? {
      name: connection.name,
      provider: connection.provider,
      base_url: connection.base_url,
      api_key: "",
      enabled: connection.enabled,
      default_adapter: connection.default_adapter,
    } : EMPTY_DRAFT);
    setEditingModel(null);
    const defaultRoute = props.settings.default_route;
    const defaultModelId = preferredModelId
      ?? (defaultRoute && connection && defaultRoute.connection_id === connection.id ? defaultRoute.model_id : "");
    setTestModelId(connection?.models.find((model) => (
      model.available && model.model_id === defaultModelId
    ))?.model_id ?? connection?.models.find((model) => model.available)?.model_id ?? "");
    setModelQuery("");
    setDeleteConfirmationOpen(false);
    setRevealedApiKey(null);
    setError("");
    setNotice("");
  };

  const selectCapability = (capability: ModelCapability) => {
    setActiveCapability(capability);
    if (capability === "primary") {
      const route = props.settings.default_route;
      const connection = route
        ? props.settings.connections.find((item) => item.id === route.connection_id)
        : props.settings.connections[0];
      if (connection) selectConnection(connection, route?.model_id);
      return;
    }
    const state = capabilityState(capability);
    const route = state?.route ?? state?.override_route ?? props.settings.default_route;
    const connection = route
      ? props.settings.connections.find((item) => item.id === route.connection_id)
      : props.settings.connections[0];
    if (connection) selectConnection(connection, route?.model_id);
  };

  const run = async (key: string, operation: () => Promise<void>) => {
    setBusy(key); setError(""); setNotice("");
    try { await operation(); } catch (reason) {
      setError(reason instanceof Error ? reason.message : "操作失败");
    } finally { setBusy(""); }
  };

  const setCapabilityMode = (mode: "follow" | "independent") => {
    if (activeCapability === "primary") return;
    const capability = activeCapability;
    if (mode === "independent") {
      setCapabilityModeOverrides((current) => ({ ...current, [capability]: mode }));
      const primaryConnectionId = props.settings.default_route?.connection_id;
      const existingIndependent = activeCapabilityState?.mode === "independent"
        ? (activeCapabilityState.route ?? activeCapabilityState.override_route)
        : null;
      // 历史配置可能把独立路由指向主连接；此时必须新建草稿，不能再覆盖主连接。
      const reusable = existingIndependent && existingIndependent.connection_id !== primaryConnectionId
        ? props.settings.connections.find((item) => item.id === existingIndependent.connection_id)
        : null;
      if (reusable) selectConnection(reusable, existingIndependent?.model_id);
      setError("");
      setNotice(reusable ? "已切换到独立连接，可编辑后保存" : "已进入独立连接配置，保存时会新增连接，不会修改主模型连接");
      return;
    }
    void run(`capability-${capability}`, async () => {
      const saved = await saveCapabilityRoute(capability, { mode: "follow" });
      setCapabilityModeOverrides((current) => ({ ...current, [capability]: mode }));
      await props.onRefresh();
      const label = capability === "vision" ? "视觉" : "Embedding";
      setNotice(saved.requires_restart === false
        ? `${label} 已跟随主模型`
        : `${label} 已跟随主模型，重启后生效`);
    });
  };

  const saveCapabilityModel = (model: ModelProfile) => {
    if (activeCapability === "primary") {
      return run("default", async () => {
        const route = await saveDefaultModelRoute({ connection_id: selected?.id || model.connection_id, model_id: model.model_id });
        setTestModelId(model.model_id);
        props.onDefaultRoute(route);
        await props.onRefresh();
        setNotice("已设为默认并切换当前会话");
      });
    }
    if (activeMode !== "independent" || !selected) return;
    const capability = activeCapability;
    return run("capability-route", async () => {
      const saved = await saveCapabilityRoute(capability, { mode: "independent", connection_id: selected.id, model_id: model.model_id });
      setTestModelId(model.model_id);
      await props.onRefresh();
      setNotice(`${capability === "vision" ? "视觉" : "Embedding"} 模型已更新，${saved.requires_restart === false ? "当前运行时已生效" : "重启后生效"}`);
    });
  };

  const saveConnection = () => run("save", async () => {
    if (activeCapability !== "primary" && activeMode === "follow") {
      throw new Error("当前能力跟随主模型，请切换为独立连接后再编辑连接信息");
    }
    const payload = { ...draft };
    if (selected && !payload.api_key) delete (payload as Partial<ConnectionDraft>).api_key;
    const isIndependent = activeCapability !== "primary" && activeMode === "independent";
    const canUpdateSelected = Boolean(selected) && (!isIndependent || selected?.id === independentRoute?.connection_id);
    const creatingConnection = !canUpdateSelected;
    // 独立能力只允许更新自己已绑定的连接；主连接或其他连接一律复制为新配置。
    const saved = canUpdateSelected
      ? await updateModelConnection(selected!.id, payload)
      : await createModelConnection(payload);
    const settings = await props.onRefresh();
    selectConnection(settings.connections.find((item) => item.id === saved.id) ?? null);
    setNotice(creatingConnection ? "新增连接成功" : "连接保存成功");
  });

  const toggleApiKeyVisibility = () => {
    if (!selected) return;
    if (revealedApiKey?.connectionId === selected.id) {
      setRevealedApiKey(null);
      return;
    }
    const connectionId = selected.id;
    void run("api-key", async () => {
      const value = await fetchModelConnectionApiKey(connectionId);
      setRevealedApiKey({ connectionId, value });
      setNotice("完整密钥已显示，切换连接后会自动隐藏");
    });
  };

  const runCapabilityTest = () => {
    if (activeCapability === "primary") return;
    const capability = activeCapability;
    const connectionId = activeMode === "follow"
      ? effectiveCapabilityRoute?.connection_id
      : selected?.id;
    const modelId = activeMode === "follow"
      ? effectiveCapabilityRoute?.model_id
      : testModel?.model_id;
    if (!connectionId || !modelId) {
      setError("请先获取并选择模型");
      return;
    }
    void run(`test-${capability}`, async () => {
      const result = await testCapability(capability, { connection_id: connectionId, model_id: modelId });
      if (capability === "embedding") {
        const dimension = result.dimensions ?? result.expected_dimension;
        setNotice(`Embedding 调用成功${dimension ? `，向量维度 ${dimension}` : ""}${result.duration_ms != null ? `，耗时 ${result.duration_ms} ms` : ""}`);
      } else {
        setNotice(`视觉模型调用成功${result.vision_received === false ? "，未检测到图片响应" : "，图片能力已验证"}${result.duration_ms != null ? `，耗时 ${result.duration_ms} ms` : ""}`);
      }
    });
  };

  return (
    <section className="model-settings-page" aria-labelledby="model-settings-title">
      <header className="model-settings-header">
        <button type="button" className="icon-button" aria-label="返回会话" title="返回会话" onClick={props.onBack}><ArrowLeft size={18} /></button>
        <div><h1 id="model-settings-title">模型连接</h1><p>管理 OpenAI-compatible 地址、密钥和模型能力</p></div>
      </header>
      <nav className="model-capability-tabs" aria-label="模型能力">
        {CAPABILITY_TABS.map((tab) => {
          const state = tab.id === "primary" ? null : capabilityState(tab.id);
          const mode = tab.id === "primary" ? "primary" : (capabilityModeOverrides[tab.id] ?? capabilityMode(state));
          const currentModel = currentModelLabel(tab.id);
          return <button
            key={tab.id}
            type="button"
            className={`model-capability-tab${activeCapability === tab.id ? " active" : ""}`}
            aria-current={activeCapability === tab.id ? "page" : undefined}
            onClick={() => selectCapability(tab.id)}
          >
            <span className="model-capability-icon">{capabilityIcon(tab.id)}</span>
            <span><strong>{tab.label}</strong><small title={currentModel}>{tab.id === "primary" ? `当前：${currentModel}` : mode === "follow" ? `跟随主模型 · ${currentModel}` : `当前：${currentModel}`}</small></span>
            {tab.id !== "primary" ? <span className={`model-capability-state ${mode === "follow" ? "follow" : "independent"}`}>{mode === "follow" ? "跟随" : "独立"}</span> : null}
          </button>;
        })}
      </nav>
      <div className="model-settings-layout">
            <aside className="connection-list">
              <button className={`connection-item ${creating ? "active" : ""}`} onClick={() => {
                if (activeCapability !== "primary") {
                  setCapabilityModeOverrides((current) => ({ ...current, [activeCapability]: "independent" }));
                }
                selectConnection(null);
              }}><Plus size={15} />新增连接</button>
              {props.settings.connections.map((connection) => (
                <button key={connection.id} className={`connection-item ${selectedId === connection.id ? "active" : ""}`} onClick={() => selectConnection(connection)}>
                  <span className={`connection-dot ${connection.enabled ? "online" : ""}`} />
                  <span><strong>{connection.name}</strong><small>{connection.models.filter((model) => model.available).length} 个模型 · {connection.has_api_key ? "Key 已配置" : "Key 未配置"}</small></span>
                </button>
              ))}
              <button className="catalog-update" disabled={Boolean(busy)} onClick={() => run("catalog", async () => {
                const result = await updateModelCatalog(); await props.onRefresh();
                setNotice(`资料库已更新，共 ${result.models} 个模型`);
              })}><Database size={15} />{busy === "catalog" ? "更新中" : "更新模型资料库"}</button>
            </aside>
            <section className="connection-editor">
              {activeCapability !== "primary" ? <section className="capability-routing-panel" aria-label={`${activeCapability === "vision" ? "视觉" : "Embedding"} 模型连接方式`}>
                <div className="capability-routing-copy">
                  <strong>{activeCapability === "vision" ? "视觉模型" : "Embedding 模型"}连接方式</strong>
                  <span>{activeCapability === "vision" ? "主模型具备图片能力时可直接复用；需要单独服务时再切换。" : "默认复用主模型连接，也可以为记忆检索选择独立向量服务。"} {activeCapabilityState?.runtime_effective_at === "now" || activeCapabilityState?.requires_restart === false ? "保存后立即生效。" : "保存路由后重启服务生效。"}{activeCapability === "embedding" && activeCapabilityState?.expected_dimension ? ` 当前向量维度 ${activeCapabilityState.expected_dimension}。` : ""}</span>
                </div>
                <div className="capability-mode-toggle" role="radiogroup" aria-label="连接方式">
                  <button type="button" role="radio" aria-checked={activeMode === "follow"} className={activeMode === "follow" ? "active" : ""} disabled={Boolean(busy)} onClick={() => setCapabilityMode("follow")}>跟随主模型</button>
                  <button type="button" role="radio" aria-checked={activeMode === "independent"} className={activeMode === "independent" ? "active" : ""} disabled={Boolean(busy)} onClick={() => setCapabilityMode("independent")}>独立连接</button>
                </div>
                {activeMode === "follow" ? <p className="capability-follow-summary">
                  当前使用：<strong>{activeCapabilityConnection?.name || "主模型默认连接"}</strong>{effectiveCapabilityRoute?.model_id ? ` / ${effectiveCapabilityRoute.model_id}` : ""}。需要更换 URL、密钥或模型时，请切换到独立连接。
                </p> : <p className="capability-follow-summary">独立模式复用连接列表中的连接；需要不同 URL 或密钥时，请先在左侧新增连接。</p>}
              </section> : null}
              <div className="connection-form-grid">
                <label><span>连接名称</span><input disabled={!connectionEditable} maxLength={80} value={draft.name} onChange={(e) => setDraft({ ...draft, name: e.target.value })} placeholder="例如：DeepSeek 官方" /></label>
                <label><span>目录供应商</span><input disabled={!connectionEditable} maxLength={80} value={draft.provider} onChange={(e) => setDraft({ ...draft, provider: e.target.value })} placeholder="models.dev provider id，可留空" /></label>
                <label className="wide"><span>Base URL</span><input disabled={!connectionEditable} value={draft.base_url} onChange={(e) => setDraft({ ...draft, base_url: e.target.value })} placeholder="https://api.example.com/v1" /></label>
                <label className="wide api-key-field"><span className="field-label"><span>API Key</span>{selected ? <span className={`api-key-state ${selected.has_api_key ? "configured" : "missing"}`}>{selected.has_api_key ? <CircleCheck size={13} /> : <KeyRound size={13} />}{selected.has_api_key ? "已配置" : "未配置"}</span> : null}</span>
                  {selected?.has_api_key ? <span className="stored-api-key">
                    <code aria-label={revealedApiKey?.connectionId === selected.id ? "API Key 完整值" : "API Key 掩码"}>{revealedApiKey?.connectionId === selected.id ? revealedApiKey.value : maskedApiKeyPreview(selected.api_key_preview)}</code>
                    <small>{revealedApiKey?.connectionId === selected.id ? "已显示完整值" : "仅显示掩码"}</small>
                    <button type="button" className="api-key-visibility" disabled={Boolean(busy)} onClick={toggleApiKeyVisibility} aria-label={revealedApiKey?.connectionId === selected.id ? "隐藏完整 API Key" : "显示完整 API Key"} title={revealedApiKey?.connectionId === selected.id ? "隐藏完整 API Key" : "显示完整 API Key"}>
                      {revealedApiKey?.connectionId === selected.id ? <EyeOff size={14} /> : <Eye size={14} />}
                      {revealedApiKey?.connectionId === selected.id ? "隐藏" : "显示"}
                    </button>
                  </span> : null}
                  <input disabled={!connectionEditable} type="password" autoComplete="new-password" value={draft.api_key} onChange={(e) => setDraft({ ...draft, api_key: e.target.value })} placeholder={selected?.has_api_key ? "输入新值可替换当前密钥" : "输入 API Key"} />
                </label>
                {activeCapability === "primary" ? <label><span>默认适配器</span><select value={draft.default_adapter} onChange={(e) => setDraft({ ...draft, default_adapter: e.target.value as ModelAdapterId })}>{ADAPTERS.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}</select></label> : <div className="capability-auto-adapter"><span>调用方式</span><strong>直接调用对应接口</strong><small>{activeCapability === "embedding" ? "使用 /embeddings 验证向量维度" : "使用图片输入验证视觉响应"}，无需手动配置适配器</small></div>}
                <label className="connection-enabled"><input disabled={!connectionEditable} type="checkbox" checked={draft.enabled} onChange={(e) => setDraft({ ...draft, enabled: e.target.checked })} /><span>启用连接</span></label>
              </div>
              <div className="connection-toolbar">
                {selected && connectionEditable ? <button className="danger-text" disabled={Boolean(busy)} onClick={() => setDeleteConfirmationOpen(true)}><Trash2 size={15} />删除</button> : null}
                <button className="primary-action" disabled={Boolean(busy) || !connectionEditable} title={!connectionEditable ? "跟随主模型时请切换到独立连接" : ""} onClick={() => void saveConnection()}><Check size={15} />{busy === "save" ? "保存中" : "保存连接"}</button>
              </div>
              {selected ? <>
                {!selected.enabled ? <p className="connection-disabled-message">连接已停用。启用并保存后，才能设为默认或调用模型。</p> : null}
                <div className="model-list-heading">
                  <div><strong>可用模型</strong><span>{selected.base_url}</span></div>
                  <button disabled={Boolean(busy)} onClick={() => run("refresh", async () => {
                    const result = await refreshConnectionModels(selected.id); await props.onRefresh();
                    setNotice(`连接“${selected.name}”已获取 ${result.items.filter((item) => item.available).length} 个模型${result.catalog_warning ? "，公共资料库暂不可用" : ""}`);
                  })}><RefreshCw size={15} />{busy === "refresh" ? "获取中" : "获取模型"}</button>
                  <button disabled={Boolean(busy)} onClick={() => run("test-list", async () => {
                    const result = await testModelConnection(selected.id);
                    setNotice(`连接“${result.connection_name}”的模型列表可用，共返回 ${result.model_count} 个模型`);
                  })}><ListChecks size={15} />{busy === "test-list" ? "测试中" : "测试模型列表"}</button>
                  {activeCapability === "primary" ? <button disabled={Boolean(busy) || !testModel} onClick={() => run("test-model", async () => {
                    if (!testModel) return;
                    const result = await testConnectionModel(selected.id, testModel.model_id, testEffort);
                    setNotice(`“${result.connection_name} / ${result.model_display_name}”调用成功${result.thinking_received ? `，思考模式 ${result.effective_effort || testEffort || "已验证"}` : ""}，耗时 ${result.duration_ms} ms`);
                  })}><Play size={15} />{busy === "test-model" ? "调用中" : "测试所选模型"}</button> : <button disabled={Boolean(busy) || !testModel || (activeMode === "follow" && !effectiveCapabilityRoute)} onClick={runCapabilityTest}><Play size={15} />{busy === `test-${activeCapability}` ? "验证中" : "测试所选模型"}</button>}
                </div>
                <div className="model-search-row">
                  <Search size={16} aria-hidden="true" />
                  <input type="search" aria-label="搜索当前连接的模型" value={modelQuery} onChange={(event) => setModelQuery(event.target.value)} placeholder="搜索模型名称或模型 ID" />
                  <span>{filteredModels.length}/{availableModels.length}</span>
                </div>
                <div className="model-profile-list">
                  {orderedModels.length ? orderedModels.map((model) => {
                    const isDefault = effectiveCapabilityRoute?.connection_id === selected.id && effectiveCapabilityRoute?.model_id === model.model_id;
                    return <div className={`model-profile-row ${testModel?.model_id === model.model_id ? "selected" : ""}`} key={model.model_id}>
                      <button className="model-profile-main" onClick={() => {
                        setTestModelId(model.model_id);
                        if (activeCapability === "primary") {
                          setEditingModel(model);
                          setContextDraft(model.context_window ? String(model.context_window) : "");
                        } else {
                          setEditingModel(null);
                        }
                      }}>
                        <strong>{model.display_name}</strong><small>{model.model_id}</small>
                      </button>
                      <span className="model-capacity">上下文 {formatCapacity(model.context_window)}</span>
                      <span className="model-source" title={model.supports_reasoning == null || (model.supports_reasoning && model.capability_confidence !== "high")
                        ? "模型资料标记支持思考，但尚未通过实际调用验证"
                        : undefined}>{model.supports_reasoning
                        ? reasoningStatusForModel(model)
                        : Object.prototype.hasOwnProperty.call(model.user_overrides, "context_window") ? "手动设置" : "思考关闭"}</span>
                      <button className={isDefault ? "default-model active" : "default-model"} disabled={Boolean(busy) || !selected.enabled || (activeCapability !== "primary" && activeMode !== "independent")} title={!selected.enabled ? "连接已停用，请先启用并保存" : activeCapability !== "primary" && activeMode !== "independent" ? "跟随主模型时不能单独选择" : ""} onClick={() => void saveCapabilityModel(model)}>{isDefault ? (activeCapability === "primary" ? "默认" : "当前") : activeCapability === "primary" ? "设为默认" : "设为当前"}</button>
                    </div>;
                  }) : <p className="model-list-empty">{availableModels.length ? "没有匹配的模型。" : "请先获取模型。"}</p>}
                </div>
                {editingModel && activeCapability === "primary" ? <div className="model-override-editor">
                  <div><strong>{editingModel.display_name}</strong><span>覆盖模型容量、推理能力与适配器</span></div>
                  <label><span>上下文 token</span><input type="number" min="1" value={contextDraft} onChange={(e) => setContextDraft(e.target.value)} placeholder="未知" /></label>
                  <label><span>适配器</span><select value={editingModel.adapter} onChange={(e) => setEditingModel({ ...editingModel, adapter: e.target.value as ModelAdapterId })}>{ADAPTERS.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}</select></label>
                  <fieldset className="model-reasoning-editor">
                    <legend>推理能力</legend>
                    <label className="model-reasoning-support"><input type="checkbox" checked={Boolean(editingModel.supports_reasoning)} onChange={(event) => setEditingModel({
                      ...editingModel,
                      supports_reasoning: event.target.checked,
                      reasoning_options: event.target.checked
                        ? (editingModel.reasoning_options.length ? editingModel.reasoning_options : ["none", "enabled"])
                        : [],
                    })} />支持推理</label>
                    {editingModel.supports_reasoning ? <div className="model-reasoning-options">
                      {REASONING_CHOICES.map((choice) => <label key={choice}><input type="checkbox" checked={editingModel.reasoning_options.includes(choice)} onChange={(event) => setEditingModel({
                        ...editingModel,
                        reasoning_options: updateReasoningOptions(editingModel.reasoning_options, choice, event.target.checked),
                      })} />{choice}</label>)}
                    </div> : null}
                  </fieldset>
                  <button disabled={Boolean(busy)} onClick={() => run("model", async () => {
                    await updateModelProfile(selected.id, editingModel.model_id, {
                      context_window: contextDraft || null,
                      adapter: editingModel.adapter,
                      supports_reasoning: Boolean(editingModel.supports_reasoning),
                      reasoning_options: editingModel.supports_reasoning ? editingModel.reasoning_options : [],
                    }); await props.onRefresh(); setEditingModel(null); setNotice("模型能力已保存");
                  })}>保存能力</button>
                </div> : null}
              </> : <div className="new-connection-empty"><Settings size={28} /><strong>新增模型连接</strong><span>保存后即可测试地址并获取模型。</span></div>}
            </section>
      </div>
      {toast ? <div className={`settings-toast ${toast.kind}`} role={toast.kind === "error" ? "alert" : "status"} aria-live={toast.kind === "error" ? "assertive" : "polite"}>
        <span aria-hidden="true">{toast.kind === "error" ? "!" : "✓"}</span><p>{toast.message}</p>
        <button type="button" aria-label="关闭提示" onClick={() => setToast(null)}>×</button>
      </div> : null}
      {selected ? <DeleteConnectionDialog
        open={deleteConfirmationOpen}
        connectionName={selected.name}
        busy={busy === "delete"}
        onOpenChange={setDeleteConfirmationOpen}
        onConfirm={() => {
          setDeleteConfirmationOpen(false);
          void run("delete", async () => {
            await deleteModelConnection(selected.id);
            const settings = await props.onRefresh();
            selectConnection(settings.connections[0] ?? null);
            setNotice("连接删除成功");
          });
        }}
      /> : null}
    </section>
  );
}

function DeleteConnectionDialog(props: {
  open: boolean;
  connectionName: string;
  busy: boolean;
  onOpenChange: (open: boolean) => void;
  onConfirm: () => void;
}) {
  return <Dialog.Root open={props.open} onOpenChange={props.onOpenChange}>
    <Dialog.Portal>
      <Dialog.Overlay className="dialog-overlay confirmation-overlay" />
      <Dialog.Content className="connection-delete-dialog">
        <TriangleAlert size={22} aria-hidden="true" />
        <div>
          <Dialog.Title>删除连接“{props.connectionName}”？</Dialog.Title>
          <Dialog.Description>将删除连接配置、已获取的模型和数据库中的 API Key，此操作无法撤销。</Dialog.Description>
        </div>
        <div className="connection-delete-actions">
          <Dialog.Close asChild><button type="button" disabled={props.busy}>取消</button></Dialog.Close>
          <button type="button" className="confirm-delete" disabled={props.busy} onClick={props.onConfirm}>{props.busy ? "删除中" : "确认删除"}</button>
        </div>
      </Dialog.Content>
    </Dialog.Portal>
  </Dialog.Root>;
}

export function formatCapacity(value: number | null | undefined): string {
  if (!value) return "--";
  if (value >= 1_000_000) return `${Number((value / 1_000_000).toFixed(1))}M`;
  if (value >= 1_000) return `${Number((value / 1_000).toFixed(value >= 100_000 ? 0 : 1))}K`;
  return String(value);
}
