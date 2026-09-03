import * as Dialog from "@radix-ui/react-dialog";
import { Check, Database, Plus, RefreshCw, Settings, Trash2, X } from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import {
  createManualModel,
  createModelConnection,
  deleteModelConnection,
  refreshConnectionModels,
  saveDefaultModelRoute,
  testModelConnection,
  updateModelCatalog,
  updateModelConnection,
  updateModelProfile,
} from "./api";
import type { ModelAdapterId, ModelConnection, ModelProfile, ModelRoute, ModelSettingsPayload } from "./types";

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

export function ModelSettingsDialog(props: {
  open: boolean;
  settings: ModelSettingsPayload;
  onOpenChange: (open: boolean) => void;
  onRefresh: () => Promise<ModelSettingsPayload>;
  onDefaultRoute: (route: ModelRoute) => void;
}) {
  const [selectedId, setSelectedId] = useState("");
  const [draft, setDraft] = useState<ConnectionDraft>(EMPTY_DRAFT);
  const [manualModel, setManualModel] = useState("");
  const [editingModel, setEditingModel] = useState<ModelProfile | null>(null);
  const [contextDraft, setContextDraft] = useState("");
  const [busy, setBusy] = useState("");
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const selected = props.settings.connections.find((item) => item.id === selectedId) ?? null;

  useEffect(() => {
    if (!props.open) return;
    const candidate = selectedId && props.settings.connections.some((item) => item.id === selectedId)
      ? selectedId : props.settings.connections[0]?.id ?? "";
    selectConnection(props.settings.connections.find((item) => item.id === candidate) ?? null);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [props.open, props.settings.connections]);

  const availableModels = useMemo(
    () => selected?.models.filter((model) => model.available) ?? [],
    [selected],
  );

  const selectConnection = (connection: ModelConnection | null) => {
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

  return (
    <Dialog.Root open={props.open} onOpenChange={props.onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="dialog-overlay" />
        <Dialog.Content className="model-settings-dialog">
          <header className="model-settings-header">
            <div><Dialog.Title>模型连接</Dialog.Title><Dialog.Description>管理 OpenAI-compatible 地址、密钥和模型能力</Dialog.Description></div>
            <Dialog.Close className="icon-button" aria-label="关闭模型设置"><X size={18} /></Dialog.Close>
          </header>
          <div className="model-settings-layout">
            <aside className="connection-list">
              <button className={`connection-item ${selectedId ? "" : "active"}`} onClick={() => selectConnection(null)}><Plus size={15} />新增连接</button>
              {props.settings.connections.map((connection) => (
                <button key={connection.id} className={`connection-item ${selectedId === connection.id ? "active" : ""}`} onClick={() => selectConnection(connection)}>
                  <span className={`connection-dot ${connection.enabled ? "online" : ""}`} />
                  <span><strong>{connection.name}</strong><small>{connection.models.filter((model) => model.available).length} 个模型</small></span>
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
                <label className="wide"><span>API Key</span><input type="password" autoComplete="new-password" value={draft.api_key} onChange={(e) => setDraft({ ...draft, api_key: e.target.value })} placeholder={selected?.has_api_key ? "已保存，留空保持不变" : "输入 API Key"} /></label>
                <label><span>默认适配器</span><select value={draft.default_adapter} onChange={(e) => setDraft({ ...draft, default_adapter: e.target.value as ModelAdapterId })}>{ADAPTERS.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}</select></label>
                <label className="connection-enabled"><input type="checkbox" checked={draft.enabled} onChange={(e) => setDraft({ ...draft, enabled: e.target.checked })} /><span>启用连接</span></label>
              </div>
              <div className="connection-toolbar">
                {selected ? <button className="danger-text" disabled={Boolean(busy)} onClick={() => run("delete", async () => {
                  await deleteModelConnection(selected.id); const settings = await props.onRefresh();
                  selectConnection(settings.connections[0] ?? null);
                })}><Trash2 size={15} />删除</button> : null}
                <button className="primary-action" disabled={Boolean(busy)} onClick={() => void saveConnection()}><Check size={15} />{busy === "save" ? "保存中" : "保存连接"}</button>
              </div>
              {selected ? <>
                <div className="model-list-heading">
                  <div><strong>可用模型</strong><span>{selected.base_url}</span></div>
                  <button disabled={Boolean(busy)} onClick={() => run("refresh", async () => {
                    const models = await refreshConnectionModels(selected.id); await props.onRefresh();
                    setNotice(`已获取 ${models.filter((item) => item.available).length} 个模型`);
                  })}><RefreshCw size={15} />{busy === "refresh" ? "获取中" : "获取模型"}</button>
                  <button disabled={Boolean(busy)} onClick={() => run("test", async () => {
                    const result = await testModelConnection(selected.id); await props.onRefresh();
                    setNotice(`连接正常，共 ${result.model_count} 个模型`);
                  })}>测试连接</button>
                </div>
                <div className="manual-model-row">
                  <input value={manualModel} onChange={(e) => setManualModel(e.target.value)} placeholder="无法自动获取时输入模型 ID" />
                  <button disabled={!manualModel.trim() || Boolean(busy)} onClick={() => run("manual", async () => {
                    await createManualModel(selected.id, { model_id: manualModel.trim() }); setManualModel(""); await props.onRefresh(); setNotice("模型已添加");
                  })}><Plus size={15} />添加</button>
                </div>
                <div className="model-profile-list">
                  {availableModels.length ? availableModels.map((model) => {
                    const isDefault = props.settings.default_route?.connection_id === selected.id && props.settings.default_route?.model_id === model.model_id;
                    return <div className="model-profile-row" key={model.model_id}>
                      <button className="model-profile-main" onClick={() => { setEditingModel(model); setContextDraft(model.context_window ? String(model.context_window) : ""); }}>
                        <strong>{model.display_name}</strong><small>{model.model_id}</small>
                      </button>
                      <span className="model-capacity">{formatCapacity(model.context_window)}</span>
                      <span className="model-source">{model.metadata_source === "unknown" ? "未匹配" : model.metadata_source}</span>
                      <button className={isDefault ? "default-model active" : "default-model"} onClick={() => run("default", async () => {
                        const route = await saveDefaultModelRoute({ connection_id: selected.id, model_id: model.model_id }); props.onDefaultRoute(route); await props.onRefresh(); setNotice("已设为默认模型");
                      })}>{isDefault ? "默认" : "设为默认"}</button>
                    </div>;
                  }) : <p className="model-list-empty">获取模型，或手工添加模型 ID。</p>}
                </div>
                {editingModel ? <div className="model-override-editor">
                  <div><strong>{editingModel.display_name}</strong><span>覆盖上下文容量与适配器</span></div>
                  <label><span>上下文 token</span><input type="number" min="1" value={contextDraft} onChange={(e) => setContextDraft(e.target.value)} placeholder="未知" /></label>
                  <label><span>适配器</span><select value={editingModel.adapter} onChange={(e) => setEditingModel({ ...editingModel, adapter: e.target.value as ModelAdapterId })}>{ADAPTERS.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}</select></label>
                  <button disabled={Boolean(busy)} onClick={() => run("model", async () => {
                    await updateModelProfile(selected.id, editingModel.model_id, { context_window: contextDraft || null, adapter: editingModel.adapter }); await props.onRefresh(); setEditingModel(null); setNotice("模型能力已保存");
                  })}>保存能力</button>
                </div> : null}
              </> : <div className="new-connection-empty"><Settings size={28} /><strong>新增模型连接</strong><span>保存后即可测试地址并获取模型。</span></div>}
              {error ? <p className="settings-message error" role="alert">{error}</p> : null}
              {notice ? <p className="settings-message" role="status">{notice}</p> : null}
            </section>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

export function formatCapacity(value: number | null | undefined): string {
  if (!value) return "--";
  if (value >= 1_000_000) return `${Number((value / 1_000_000).toFixed(1))}M`;
  if (value >= 1_000) return `${Number((value / 1_000).toFixed(value >= 100_000 ? 0 : 1))}K`;
  return String(value);
}
