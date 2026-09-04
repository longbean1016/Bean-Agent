import * as Dialog from "@radix-ui/react-dialog";
import { ArrowLeft, Check, CircleCheck, Copy, Database, Eye, EyeOff, KeyRound, ListChecks, Play, Plus, RefreshCw, Search, Settings, Trash2, TriangleAlert } from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import {
  createModelConnection,
  deleteModelConnection,
  fetchModelConnectionApiKey,
  refreshConnectionModels,
  saveDefaultModelRoute,
  testModelConnection,
  testConnectionModel,
  updateModelCatalog,
  updateModelConnection,
  updateModelProfile,
} from "./api";
import type { ModelAdapterId, ModelConnection, ModelProfile, ModelRoute, ModelSettingsPayload } from "./types";
import { REASONING_CHOICES, updateReasoningOptions } from "./reasoning";

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

const EMPTY_DRAFT: ConnectionDraft = {
  name: "", provider: "", base_url: "", api_key: "", enabled: true,
  default_adapter: "generic_openai",
};

export function ModelSettingsPage(props: {
  settings: ModelSettingsPayload;
  onBack: () => void;
  onRefresh: () => Promise<ModelSettingsPayload>;
  onDefaultRoute: (route: ModelRoute) => void;
}) {
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
  const [revealedApiKey, setRevealedApiKey] = useState("");
  const [showApiKey, setShowApiKey] = useState(false);
  const [deleteConfirmationOpen, setDeleteConfirmationOpen] = useState(false);
  const selected = props.settings.connections.find((item) => item.id === selectedId) ?? null;

  useEffect(() => {
    if (creating) return;
    if (selectedId && props.settings.connections.some((item) => item.id === selectedId)) return;
    selectConnection(props.settings.connections[0] ?? null);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [creating, props.settings.connections, selectedId]);

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
  const testModel = availableModels.find((model) => model.model_id === testModelId)
    ?? availableModels.find((model) => props.settings.default_route?.connection_id === selected?.id && props.settings.default_route?.model_id === model.model_id)
    ?? availableModels[0];

  const selectConnection = (connection: ModelConnection | null) => {
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
    const defaultModelId = defaultRoute && connection && defaultRoute.connection_id === connection.id
      ? defaultRoute.model_id
      : "";
    setTestModelId(connection?.models.find((model) => (
      model.available && model.model_id === defaultModelId
    ))?.model_id ?? connection?.models.find((model) => model.available)?.model_id ?? "");
    setModelQuery("");
    setRevealedApiKey("");
    setShowApiKey(false);
    setDeleteConfirmationOpen(false);
    setError("");
    setNotice("");
  };

  const run = async (key: string, operation: () => Promise<void>) => {
    setBusy(key); setError(""); setNotice("");
    try { await operation(); } catch (reason) {
      setError(reason instanceof Error ? reason.message : "操作失败");
    } finally { setBusy(""); }
  };

  const saveConnection = () => run("save", async () => {
    const payload = { ...draft };
    if (selected && !payload.api_key) delete (payload as Partial<ConnectionDraft>).api_key;
    const saved = selected
      ? await updateModelConnection(selected.id, payload)
      : await createModelConnection(payload);
    const settings = await props.onRefresh();
    selectConnection(settings.connections.find((item) => item.id === saved.id) ?? null);
    setNotice("连接已保存");
  });

  const loadApiKey = async (): Promise<string> => {
    if (!selected) throw new Error("请先选择连接");
    if (revealedApiKey) return revealedApiKey;
    const apiKey = await fetchModelConnectionApiKey(selected.id);
    setRevealedApiKey(apiKey);
    return apiKey;
  };

  const toggleApiKey = () => run("api-key", async () => {
    if (showApiKey) {
      setShowApiKey(false);
      return;
    }
    await loadApiKey();
    setShowApiKey(true);
  });

  const copyApiKey = () => run("copy-key", async () => {
    const apiKey = await loadApiKey();
    await navigator.clipboard.writeText(apiKey);
    setNotice("API Key 已复制");
  });

  return (
    <section className="model-settings-page" aria-labelledby="model-settings-title">
      <header className="model-settings-header">
        <button type="button" className="icon-button" aria-label="返回会话" title="返回会话" onClick={props.onBack}><ArrowLeft size={18} /></button>
        <div><h1 id="model-settings-title">模型连接</h1><p>管理 OpenAI-compatible 地址、密钥和模型能力</p></div>
      </header>
      <div className="model-settings-layout">
            <aside className="connection-list">
              <button className={`connection-item ${creating ? "active" : ""}`} onClick={() => selectConnection(null)}><Plus size={15} />新增连接</button>
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
              <div className="connection-form-grid">
                <label><span>连接名称</span><input maxLength={80} value={draft.name} onChange={(e) => setDraft({ ...draft, name: e.target.value })} placeholder="例如：DeepSeek 官方" /></label>
                <label><span>目录供应商</span><input maxLength={80} value={draft.provider} onChange={(e) => setDraft({ ...draft, provider: e.target.value })} placeholder="models.dev provider id，可留空" /></label>
                <label className="wide"><span>Base URL</span><input value={draft.base_url} onChange={(e) => setDraft({ ...draft, base_url: e.target.value })} placeholder="https://api.example.com/v1" /></label>
                <label className="wide api-key-field"><span className="field-label"><span>API Key</span>{selected ? <span className={`api-key-state ${selected.has_api_key ? "configured" : "missing"}`}>{selected.has_api_key ? <CircleCheck size={13} /> : <KeyRound size={13} />}{selected.has_api_key ? "已配置" : "未配置"}</span> : null}</span>
                  {selected?.has_api_key ? <span className="stored-api-key">
                    <code>{showApiKey ? revealedApiKey : selected.api_key_preview || "已保存"}</code>
                    <button type="button" className="icon-button" title={showApiKey ? "隐藏 API Key" : "明文查看 API Key"} aria-label={showApiKey ? "隐藏 API Key" : "明文查看 API Key"} disabled={Boolean(busy)} onClick={() => void toggleApiKey()}>{showApiKey ? <EyeOff size={16} /> : <Eye size={16} />}</button>
                    <button type="button" className="icon-button" title="复制 API Key" aria-label="复制 API Key" disabled={Boolean(busy)} onClick={() => void copyApiKey()}><Copy size={16} /></button>
                  </span> : null}
                  <input type="password" autoComplete="new-password" value={draft.api_key} onChange={(e) => setDraft({ ...draft, api_key: e.target.value })} placeholder={selected?.has_api_key ? "输入新值可替换当前密钥" : "输入 API Key"} />
                </label>
                <label><span>默认适配器</span><select value={draft.default_adapter} onChange={(e) => setDraft({ ...draft, default_adapter: e.target.value as ModelAdapterId })}>{ADAPTERS.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}</select></label>
                <label className="connection-enabled"><input type="checkbox" checked={draft.enabled} onChange={(e) => setDraft({ ...draft, enabled: e.target.checked })} /><span>启用连接</span></label>
              </div>
              <div className="connection-toolbar">
                {selected ? <button className="danger-text" disabled={Boolean(busy)} onClick={() => setDeleteConfirmationOpen(true)}><Trash2 size={15} />删除</button> : null}
                <button className="primary-action" disabled={Boolean(busy)} onClick={() => void saveConnection()}><Check size={15} />{busy === "save" ? "保存中" : "保存连接"}</button>
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
                  <button disabled={Boolean(busy) || !testModel} onClick={() => run("test-model", async () => {
                    if (!testModel) return;
                    const result = await testConnectionModel(selected.id, testModel.model_id);
                    setNotice(`“${result.connection_name} / ${result.model_display_name}”调用成功，耗时 ${result.duration_ms} ms`);
                  })}><Play size={15} />{busy === "test-model" ? "调用中" : "测试所选模型"}</button>
                </div>
                {error ? <p className="model-test-message error" role="alert">{error}</p> : null}
                {notice ? <p className="model-test-message" role="status">{notice}</p> : null}
                <div className="model-search-row">
                  <Search size={16} aria-hidden="true" />
                  <input type="search" aria-label="搜索当前连接的模型" value={modelQuery} onChange={(event) => setModelQuery(event.target.value)} placeholder="搜索模型名称或模型 ID" />
                  <span>{filteredModels.length}/{availableModels.length}</span>
                </div>
                <div className="model-profile-list">
                  {filteredModels.length ? filteredModels.map((model) => {
                    const isDefault = props.settings.default_route?.connection_id === selected.id && props.settings.default_route?.model_id === model.model_id;
                    return <div className={`model-profile-row ${testModel?.model_id === model.model_id ? "selected" : ""}`} key={model.model_id}>
                      <button className="model-profile-main" onClick={() => { setTestModelId(model.model_id); setEditingModel(model); setContextDraft(model.context_window ? String(model.context_window) : ""); }}>
                        <strong>{model.display_name}</strong><small>{model.model_id}</small>
                      </button>
                      <span className="model-capacity">上下文 {formatCapacity(model.context_window)}</span>
                      <span className="model-source">{model.supports_reasoning && model.reasoning_options.length
                        ? `推理 ${model.reasoning_options.join("/")}`
                        : Object.prototype.hasOwnProperty.call(model.user_overrides, "context_window") ? "手动设置" : model.metadata_source === "unknown" ? "未匹配" : model.metadata_source}</span>
                      <button className={isDefault ? "default-model active" : "default-model"} disabled={Boolean(busy) || !selected.enabled} title={!selected.enabled ? "连接已停用，请先启用并保存" : ""} onClick={() => run("default", async () => {
                        const route = await saveDefaultModelRoute({ connection_id: selected.id, model_id: model.model_id }); setTestModelId(model.model_id); props.onDefaultRoute(route); await props.onRefresh(); setNotice("已设为默认并切换当前会话");
                      })}>{isDefault ? "默认" : "设为默认"}</button>
                    </div>;
                  }) : <p className="model-list-empty">{availableModels.length ? "没有匹配的模型。" : "请先获取模型。"}</p>}
                </div>
                {editingModel ? <div className="model-override-editor">
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
              {!selected && error ? <p className="settings-message error" role="alert">{error}</p> : null}
              {!selected && notice ? <p className="settings-message" role="status">{notice}</p> : null}
            </section>
      </div>
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
