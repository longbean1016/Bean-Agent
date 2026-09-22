import {
  AlertTriangle,
  ArrowLeft,
  CheckCircle2,
  FolderOpen,
  Globe2,
  Monitor,
  Package,
  PlugZap,
  Plus,
  Puzzle,
  RefreshCw,
  Search,
  Sparkles,
  Terminal,
  Trash2,
  Upload,
  Wrench,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import { createMcpExtension, discoverMcpSources, fetchMcpExtensions, fetchPluginExtensions, fetchSkillExtensions, importDiscoveredMcpExtensions, importMcpExtensions, refreshMcpExtension, removeMcpExtension, setMcpEnabled, updateMcpExtension } from "./api";
import type { McpExtensionConfigPayload } from "./api";
import type { ExtensionScope, McpExtensionRecord, PluginExtensionRecord, SkillExtensionRecord } from "./types";
import type { ExtensionKind } from "./chatRoute";

type ExtensionRecord = PluginExtensionRecord | McpExtensionRecord | SkillExtensionRecord;

const pageMeta: Record<ExtensionKind, { title: string; subtitle: string; icon: typeof Puzzle; empty: string }> = {
  plugins: { title: "插件", subtitle: "安装和管理可扩展能力；插件可以提供 Skills、MCP 和 Commands。", icon: Puzzle, empty: "尚未安装插件" },
  mcp: { title: "MCP", subtitle: "连接外部工具服务，让 BeanAgent 获得更多可调用工具。", icon: PlugZap, empty: "尚未配置 MCP 服务" },
  skills: { title: "技能 Skills", subtitle: "管理可复用的提示词与工作流能力，来源和依赖状态清晰可见。", icon: Sparkles, empty: "尚未发现技能" },
};

export function ExtensionsPage({
  kind,
  onBack,
}: {
  kind: ExtensionKind;
  onBack: () => void;
}) {
  const [scope, setScope] = useState<ExtensionScope>("workspace");
  const [query, setQuery] = useState("");
  const [records, setRecords] = useState<ExtensionRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [mcpDialogOpen, setMcpDialogOpen] = useState(false);
  const [mcpImportOpen, setMcpImportOpen] = useState(false);
  const [mcpImportText, setMcpImportText] = useState("");
  const [mcpEditing, setMcpEditing] = useState<McpExtensionRecord | null>(null);
  const [mcpName, setMcpName] = useState("");
  const [mcpType, setMcpType] = useState<"stdio" | "http" | "sse">("stdio");
  const [mcpCommand, setMcpCommand] = useState("");
  const [mcpUrl, setMcpUrl] = useState("");
  const [mcpHeaders, setMcpHeaders] = useState("");
  const [mcpOauth, setMcpOauth] = useState("");
  const [mcpCwd, setMcpCwd] = useState("");
  const [mcpTimeout, setMcpTimeout] = useState("");
  const [mcpSaving, setMcpSaving] = useState(false);
  // 页面类型切换时旧请求可能晚于新请求返回；只有最新请求可以提交列表状态。
  const refreshVersionRef = useRef(0);
  const meta = pageMeta[kind];
  const openCreateMcp = () => {
    setMcpEditing(null);
    setMcpName("");
    setMcpType("stdio");
    setMcpCommand("");
    setMcpUrl("");
    setMcpHeaders("");
    setMcpOauth("");
    setMcpCwd("");
    setMcpTimeout("");
    setMcpDialogOpen(true);
  };

  const refresh = async () => {
    const requestVersion = ++refreshVersionRef.current;
    setLoading(true);
    setError("");
    try {
      const next = kind === "plugins"
        ? await fetchPluginExtensions(scope)
        : kind === "mcp"
          ? await fetchMcpExtensions(scope)
          : await fetchSkillExtensions(scope);
      if (requestVersion !== refreshVersionRef.current) return;
      setRecords(Array.isArray(next) ? next : []);
    } catch (reason) {
      if (requestVersion !== refreshVersionRef.current) return;
      setError(reason instanceof Error ? reason.message : "扩展列表加载失败");
      setRecords([]);
    } finally {
      if (requestVersion === refreshVersionRef.current) setLoading(false);
    }
  };

  useEffect(() => {
    void refresh();
    return () => { refreshVersionRef.current += 1; };
  }, [kind, scope]);

  const filtered = useMemo(() => {
    const normalized = query.trim().toLocaleLowerCase();
    if (!normalized) return records;
    return records.filter((record) => {
      const source = "source" in record ? record.source : "transport" in record ? record.transport : "";
      return `${record.name} ${record.description} ${source}`.toLocaleLowerCase().includes(normalized);
    });
  }, [query, records]);
  const Icon = meta.icon;

  return (
    <div className="extensions-page">
      <header className="extensions-header">
        <div className="extensions-heading">
          <button className="icon-button extensions-back" aria-label="返回聊天" title="返回聊天" onClick={onBack}><ArrowLeft size={18} /></button>
          <div className="extensions-heading-icon"><Icon size={19} /></div>
          <div><h1>扩展</h1><p>插件、MCP 与技能管理</p></div>
        </div>
        <div className="extensions-header-actions"><span className="extension-service-state"><span />服务已连接</span></div>
      </header>

      <main className="extensions-content">
        <div className="extensions-toolbar">
          <div className="extension-scope" role="group" aria-label="扩展范围">
            <button className={scope === "user" ? "active" : ""} onClick={() => setScope("user")}><Monitor size={14} />用户级</button>
            <button className={scope === "workspace" ? "active" : ""} onClick={() => setScope("workspace")}><FolderOpen size={14} />工作区级</button>
          </div>
          <div className="extensions-type-label"><Icon size={16} />{meta.title}<span>{filtered.length}</span></div>
          <label className="extensions-search"><Search size={16} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder={`搜索${meta.title}…`} aria-label={`搜索${meta.title}`} /></label>
        </div>

        <div className="extensions-actions-row">
          <div><h2>{kind === "plugins" ? "已安装" : kind === "mcp" ? "已配置" : "已安装"} <small>{filtered.length}</small></h2><span className="extensions-scope-hint">{scope === "workspace" ? "当前工作区" : "当前用户"}</span></div>
          <div className="extensions-actions">
            <button className="icon-button" aria-label="刷新扩展" title="刷新" onClick={() => void refresh()} disabled={loading}><RefreshCw size={16} className={loading ? "spin" : ""} /></button>
            <button className="secondary-action" onClick={() => { if (kind === "mcp") setMcpImportOpen(true); }} disabled={kind !== "mcp"}><Upload size={15} />导入{kind === "mcp" ? " JSON" : ""}</button>
            <button className="primary-action" onClick={() => { if (kind === "mcp") openCreateMcp(); }} disabled={kind !== "mcp"}><Plus size={16} />新建</button>
          </div>
        </div>

        {error ? <div className="extensions-error" role="alert"><AlertTriangle size={16} />{error}<button onClick={() => void refresh()}>重试</button></div> : null}
        {loading ? <div className="extensions-loading">正在加载扩展…</div> : filtered.length === 0 ? (
          <div className="extensions-empty"><Icon size={30} /><strong>{meta.empty}</strong><span>{kind === "mcp" ? "可以通过导入 JSON 或新建服务开始配置。" : "可以通过导入或新建开始配置。"}</span></div>
        ) : (
          <div className={`extensions-list extensions-list-${kind}`}>
            {filtered.map((record) => <ExtensionRow key={record.id} kind={kind} record={record} onRemoveMcp={async (item) => { await removeMcpExtension(item.name, scope, item.revision); await refresh(); }} onEditMcp={(item) => { setMcpEditing(item); setMcpName(item.name); setMcpType(item.transport === "http" || item.transport === "sse" ? item.transport : "stdio"); setMcpCommand(item.command || ""); setMcpUrl(item.url || ""); setMcpHeaders(""); setMcpOauth(""); setMcpCwd(item.cwd || ""); setMcpTimeout(item.timeout_ms ? String(item.timeout_ms) : ""); setMcpDialogOpen(true); }} onToggleMcp={async (item) => { await setMcpEnabled(item.id, scope, !item.enabled, item.revision); await refresh(); }} onRefreshMcp={async (item) => { await refreshMcpExtension(item.id, scope); await refresh(); }} />)}
          </div>
        )}
      </main>
      <McpCreateDialog
        open={mcpDialogOpen}
        editing={mcpEditing}
        saving={mcpSaving}
        name={mcpName}
        type={mcpType}
        command={mcpCommand}
        url={mcpUrl}
        headers={mcpHeaders}
        oauth={mcpOauth}
        cwd={mcpCwd}
        timeout={mcpTimeout}
        onName={setMcpName}
        onType={setMcpType}
        onCommand={setMcpCommand}
        onUrl={setMcpUrl}
        onHeaders={setMcpHeaders}
        onOauth={setMcpOauth}
        onCwd={setMcpCwd}
        onTimeout={setMcpTimeout}
        onClose={() => { if (!mcpSaving) setMcpDialogOpen(false); }}
        onSubmit={async () => {
          if (!mcpName.trim() || (mcpType === "stdio" ? !mcpCommand.trim() : !mcpUrl.trim()) || mcpSaving) return;
          setMcpSaving(true);
          setError("");
          try {
            let headers: Record<string, string> | undefined;
            if (mcpType !== "stdio" && mcpHeaders.trim()) {
              const parsed = JSON.parse(mcpHeaders) as unknown;
              if (!parsed || typeof parsed !== "object" || Array.isArray(parsed) || Object.entries(parsed as Record<string, unknown>).some(([, value]) => typeof value !== "string")) throw new Error("请求头必须是字符串键值 JSON");
              headers = parsed as Record<string, string>;
            }
            let oauth: Record<string, unknown> | undefined;
            if (mcpType !== "stdio" && mcpOauth.trim()) {
              const parsed = JSON.parse(mcpOauth) as unknown;
              if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("OAuth 配置必须是 JSON 对象");
              oauth = parsed as Record<string, unknown>;
            }
            const payload: McpExtensionConfigPayload = { name: mcpName.trim(), scope, revision: mcpEditing?.revision, type: mcpType, cwd: mcpCwd.trim() || undefined, url: mcpType === "stdio" ? undefined : mcpUrl.trim(), headers, oauth, command: mcpType === "stdio" ? mcpCommand.trim().split(/\s+/u) : undefined, timeoutMs: mcpTimeout.trim() ? Number(mcpTimeout) : undefined };
            if (mcpEditing) await updateMcpExtension(mcpEditing.id, scope, payload); else await createMcpExtension(payload);
            setMcpDialogOpen(false);
            setMcpEditing(null); setMcpName(""); setMcpCommand(""); setMcpUrl(""); setMcpHeaders(""); setMcpOauth(""); setMcpCwd(""); setMcpTimeout("");
            await refresh();
          } catch (reason) {
            setError(reason instanceof Error ? reason.message : "无法添加 MCP 服务");
          } finally { setMcpSaving(false); }
        }}
      />
      <McpImportDialog open={mcpImportOpen} text={mcpImportText} onText={setMcpImportText} onClose={() => setMcpImportOpen(false)} onImport={async () => { const parsed = JSON.parse(mcpImportText) as unknown; const result = await importMcpExtensions(parsed, scope); if (result.failed.length) setError(`导入完成：成功 ${result.imported.length}，跳过 ${result.skipped.length}，失败 ${result.failed.length}`); setMcpImportOpen(false); setMcpImportText(""); await refresh(); }} onImportSelections={async (selections) => { const result = await importDiscoveredMcpExtensions(selections, scope); if (result.failed.length) setError(`导入完成：成功 ${result.imported.length}，跳过 ${result.skipped.length}，失败 ${result.failed.length}`); setMcpImportOpen(false); await refresh(); }} />
    </div>
  );
}

function ExtensionRow({ kind, record, onRemoveMcp, onEditMcp, onToggleMcp, onRefreshMcp }: { kind: ExtensionKind; record: ExtensionRecord; onRemoveMcp: (record: McpExtensionRecord) => Promise<void>; onEditMcp: (record: McpExtensionRecord) => void; onToggleMcp: (record: McpExtensionRecord) => Promise<void>; onRefreshMcp: (record: McpExtensionRecord) => Promise<void> }) {
  if (kind === "plugins") return <PluginRow record={record as PluginExtensionRecord} />;
  if (kind === "mcp") return <McpRow record={record as McpExtensionRecord} onRemove={onRemoveMcp} onEdit={onEditMcp} onToggle={onToggleMcp} onRefresh={onRefreshMcp} />;
  return <SkillRow record={record as SkillExtensionRecord} />;
}

function PluginRow({ record }: { record: PluginExtensionRecord }) {
  return (
    <article className="extension-row">
      <div className="extension-row-icon plugin-icon"><Package size={21} /></div>
      <div className="extension-row-copy"><strong>{record.name}</strong><p>{record.description}</p><div className="extension-tags"><span>Skills {record.skills_count}</span><span>MCP {record.mcp_count}</span><span>Commands {record.commands_count}</span><small>v{record.version} · {record.source}</small></div>{record.mcp_groups?.length ? <div className="extension-tool-list plugin-mcp-group-list">{record.mcp_groups.map((mcp) => <span key={mcp.id}><PlugZap size={11} />{mcp.name} · {mcp.status === "connected" ? "已连接" : mcp.status === "disabled" ? "插件已停用" : "未加载"}</span>)}</div> : null}</div>
      <span className="extension-status success"><CheckCircle2 size={14} />已启用</span>
      <button className="icon-button" aria-label={`删除插件 ${record.name}`} title="删除插件" disabled><Trash2 size={16} /></button>
    </article>
  );
}

function McpRow({ record, onRemove, onEdit, onToggle, onRefresh }: { record: McpExtensionRecord; onRemove: (record: McpExtensionRecord) => Promise<void>; onEdit: (record: McpExtensionRecord) => void; onToggle: (record: McpExtensionRecord) => Promise<void>; onRefresh: (record: McpExtensionRecord) => Promise<void> }) {
  const connected = record.status === "connected";
  const transport = typeof record.transport === "string" ? record.transport : "stdio";
  const [removing, setRemoving] = useState(false);
  const [busy, setBusy] = useState(false);
  const statusLabel = record.authorization_required ? "需要授权" : record.status === "error" ? "连接错误" : record.status === "connecting" ? "连接中" : record.status === "disabled" ? "已停用" : connected ? "已连接" : "未连接";
  return (
    <article className={`extension-row mcp-row ${connected ? "is-connected" : ""}`} onDoubleClick={() => onEdit(record)}>
      <div className="extension-row-icon mcp-icon">{transport === "stdio" ? <Terminal size={21} /> : <Globe2 size={21} />}</div>
      <div className="extension-row-copy"><strong>{record.name}</strong><p>{record.description} · {transport.toUpperCase()} · {record.tool_count} 个工具</p><div className="extension-detail-line"><span>{record.url || record.command || "未配置连接地址"}</span>{record.cwd ? <span>{record.cwd}</span> : null}{record.env_names?.length ? <span>环境变量 {record.env_names.length} 项（已脱敏）</span> : null}{record.header_names?.length ? <span>请求头 {record.header_names.length} 项（已脱敏）</span> : null}</div>{record.tools?.length ? <div className="extension-tool-list">{record.tools.map((tool) => <span key={tool}><Wrench size={11} />{tool}</span>)}</div> : null}{record.error ? <div className="extension-warning"><AlertTriangle size={13} />{record.error}</div> : null}</div>
      <span className={`extension-status ${connected ? "success" : record.status === "error" ? "warning" : "muted"}`}><span className="status-dot" />{statusLabel}</span>
      <div className="extension-row-actions"><button className="icon-button" aria-label={`刷新 MCP ${record.name}`} title="刷新工具" disabled={busy || record.status === "disabled"} onClick={() => { setBusy(true); void onRefresh(record).finally(() => setBusy(false)); }}><RefreshCw size={15} className={busy ? "spin" : ""} /></button><button className="icon-button" aria-label={record.enabled ? `停用 MCP ${record.name}` : `启用 MCP ${record.name}`} title={record.enabled ? "停用" : "启用"} disabled={busy} onClick={() => { setBusy(true); void onToggle(record).finally(() => setBusy(false)); }}>{record.enabled ? <CheckCircle2 size={15} /> : <PlugZap size={15} />}</button><button className="icon-button" aria-label={`编辑 MCP ${record.name}`} title="编辑" onClick={() => onEdit(record)}><Wrench size={15} /></button><button className="icon-button" aria-label={`移除 MCP ${record.name}`} title="移除 MCP" disabled={removing} onClick={() => { if (!window.confirm(`确认移除 MCP「${record.name}」？将注销 ${record.tool_count} 个工具。`)) return; setRemoving(true); void onRemove(record).finally(() => setRemoving(false)); }}><Trash2 size={16} /></button></div>
    </article>
  );
}

function McpCreateDialog({ open, editing, saving, name, type, command, url, headers, oauth, cwd, timeout, onName, onType, onCommand, onUrl, onHeaders, onOauth, onCwd, onTimeout, onClose, onSubmit }: { open: boolean; editing: McpExtensionRecord | null; saving: boolean; name: string; type: "stdio" | "http" | "sse"; command: string; url: string; headers: string; oauth: string; cwd: string; timeout: string; onName: (value: string) => void; onType: (value: "stdio" | "http" | "sse") => void; onCommand: (value: string) => void; onUrl: (value: string) => void; onHeaders: (value: string) => void; onOauth: (value: string) => void; onCwd: (value: string) => void; onTimeout: (value: string) => void; onClose: () => void; onSubmit: () => Promise<void> }) {
  if (!open) return null;
  const isStdio = type === "stdio";
  return (
    <div className="extensions-dialog-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
      <form className="extensions-dialog" onSubmit={(event) => { event.preventDefault(); void onSubmit(); }}>
        <header><div><strong>{editing ? "编辑 MCP" : "新建 MCP"}</strong><span>先测试连接和工具发现，再保存配置</span></div><button type="button" className="icon-button" onClick={onClose} aria-label="关闭">×</button></header>
        <label>服务名称<input value={name} disabled={Boolean(editing)} onChange={(event) => onName(event.target.value)} placeholder="filesystem" autoFocus /></label>
        <label>传输类型<select value={type} onChange={(event) => onType(event.target.value as "stdio" | "http" | "sse")}><option value="stdio">stdio（本地命令）</option><option value="http">HTTP（Streamable HTTP）</option><option value="sse">SSE</option></select></label>
        {isStdio ? (
          <><label>启动命令<input value={command} onChange={(event) => onCommand(event.target.value)} placeholder="npx -y @modelcontextprotocol/server-filesystem ." /></label><label>工作目录（可选）<input value={cwd} onChange={(event) => onCwd(event.target.value)} placeholder="D:\\code\\Agent_change" /></label></>
        ) : (
          <><label>服务 URL<input value={url} onChange={(event) => onUrl(event.target.value)} placeholder="https://example.com/mcp" /></label><label>请求头 JSON（可选）<textarea rows={3} value={headers} onChange={(event) => onHeaders(event.target.value)} placeholder='{"Authorization":"Bearer …"}' /><small>密钥只提交给后端，列表不会回显值。</small></label><label>OAuth JSON（可选）<textarea rows={4} value={oauth} onChange={(event) => onOauth(event.target.value)} placeholder='{"mode":"client_credentials","token_url":"https://example.com/oauth/token","client_id":"beanagent","client_secret_ref":"mcp/example/client-secret"}' /><small>client_secret_ref 只引用后端安全存储，不在此处填写密钥。</small></label></>
        )}
        <label>超时毫秒（可选）<input type="number" min="1" value={timeout} onChange={(event) => onTimeout(event.target.value)} placeholder="30000" /></label>
        <footer><button type="button" className="secondary-action" onClick={onClose} disabled={saving}>取消</button><button type="submit" className="primary-action" disabled={saving || !name.trim() || (isStdio ? !command.trim() : !url.trim())}>{saving ? "连接中…" : editing ? "测试并保存" : "连接并保存"}</button></footer>
      </form>
    </div>
  );
}

function McpImportDialog({ open, text, onText, onClose, onImport, onImportSelections }: { open: boolean; text: string; onText: (value: string) => void; onClose: () => void; onImport: () => Promise<void>; onImportSelections: (selections: Array<{ source_id: string; names: string[] }>) => Promise<void> }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [sources, setSources] = useState<Awaited<ReturnType<typeof discoverMcpSources>>>([]);
  const [selected, setSelected] = useState<Record<string, string[]>>({});
  const [discovering, setDiscovering] = useState(false);
  if (!open) return null;
  const selectedCount = Object.values(selected).reduce((sum, names) => sum + names.length, 0);
  const scan = () => { setDiscovering(true); setError(""); void discoverMcpSources().then((next) => { setSources(next); setSelected(Object.fromEntries(next.map((source) => [source.id, source.servers.map((server) => server.name)]))); }).catch((reason) => setError(reason instanceof Error ? reason.message : "发现失败")).finally(() => setDiscovering(false)); };
  const importSelected = () => { const selections = Object.entries(selected).filter(([, names]) => names.length).map(([source_id, names]) => ({ source_id, names })); setBusy(true); setError(""); void onImportSelections(selections).catch((reason) => setError(reason instanceof Error ? reason.message : "导入失败")).finally(() => setBusy(false)); };
  return <div className="extensions-dialog-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget && !busy) onClose(); }}><form className="extensions-dialog" onSubmit={(event) => { event.preventDefault(); setBusy(true); setError(""); void onImport().catch((reason) => setError(reason instanceof Error ? reason.message : "导入失败")).finally(() => setBusy(false)); }}><header><div><strong>导入 MCP</strong><span>支持 JSON 或从外部 Agent 配置中选择</span></div><button type="button" className="icon-button" onClick={onClose} aria-label="关闭" disabled={busy}>×</button></header><div className="extensions-import-discovery"><button type="button" className="secondary-action" onClick={scan} disabled={busy || discovering}>{discovering ? "扫描中…" : "扫描外部 Agent"}</button>{sources.length ? <span>发现 {sources.length} 个来源，已选 {selectedCount} 个服务</span> : <span>不会上传密钥，只读取本机配置摘要</span>}</div>{sources.length ? <div className="extensions-source-list">{sources.map((source) => <section key={source.id} className="extensions-source"><header><strong>{source.agent}</strong><small>{source.scope === "project" ? "项目" : "全局"} · {source.path}</small></header>{source.servers.map((server) => { const checked = selected[source.id]?.includes(server.name) ?? false; return <label key={server.name}><input type="checkbox" checked={checked} onChange={() => setSelected((current) => ({ ...current, [source.id]: checked ? (current[source.id] ?? []).filter((name) => name !== server.name) : [...(current[source.id] ?? []), server.name] }))} /><span>{server.name} · {server.transport}</span><small>{server.summary || "未提供摘要"}{server.has_secrets ? " · 含敏感配置" : ""}</small></label>; })}</section>)}</div> : null}<label>配置 JSON<textarea rows={8} value={text} onChange={(event) => onText(event.target.value)} placeholder={'{"mcpServers":{"filesystem":{"type":"stdio","command":["npx"],"args":["-y","server-filesystem"]}}}'} /></label>{error ? <div className="extensions-error" role="alert">{error}</div> : null}<footer><button type="button" className="secondary-action" onClick={onClose} disabled={busy}>取消</button>{sources.length ? <button type="button" className="secondary-action" onClick={importSelected} disabled={busy || selectedCount === 0}>{busy ? "导入中…" : "导入所选"}</button> : null}<button type="submit" className="primary-action" disabled={busy || !text.trim()}>{busy ? "导入中…" : "导入 JSON"}</button></footer></form></div>;
}

function SkillRow({ record }: { record: SkillExtensionRecord }) {
  const ready = record.available && !record.missing;
  return (
    <article className="extension-row skill-row">
      <div className="extension-row-icon skill-icon"><Sparkles size={21} /></div>
      <div className="extension-row-copy"><strong>{record.name}</strong><p>{record.description}</p><div className="extension-tags"><span>{record.source === "builtin" ? "内置" : record.source === "workspace" ? "工作区" : record.source}</span>{record.always ? <span>始终启用</span> : null}{record.plugin_name ? <span>插件：{record.plugin_name}</span> : null}</div>{record.missing ? <div className="extension-warning"><AlertTriangle size={13} />缺少依赖：{record.missing}</div> : null}</div>
      <span className={`extension-status ${ready ? "success" : "warning"}`}>{ready ? <CheckCircle2 size={14} /> : <AlertTriangle size={14} />}{ready ? "可用" : "需处理"}</span>
      <button className="icon-button" aria-label={`删除技能 ${record.name}`} title="删除技能" disabled={record.source === "builtin"}><Trash2 size={16} /></button>
    </article>
  );
}

export function extensionIcon(kind: ExtensionKind) {
  return kind === "plugins" ? Puzzle : kind === "mcp" ? PlugZap : Sparkles;
}
