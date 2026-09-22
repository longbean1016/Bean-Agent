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

import { createMcpExtension, fetchMcpExtensions, fetchPluginExtensions, fetchSkillExtensions, removeMcpExtension } from "./api";
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
  const [mcpName, setMcpName] = useState("");
  const [mcpCommand, setMcpCommand] = useState("");
  const [mcpCwd, setMcpCwd] = useState("");
  const [mcpSaving, setMcpSaving] = useState(false);
  // 页面类型切换时旧请求可能晚于新请求返回；只有最新请求可以提交列表状态。
  const refreshVersionRef = useRef(0);
  const meta = pageMeta[kind];

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
            <button className="secondary-action" disabled><Upload size={15} />导入{kind === "mcp" ? " JSON" : ""}</button>
            <button className="primary-action" onClick={() => { if (kind === "mcp") setMcpDialogOpen(true); }} disabled={kind !== "mcp"}><Plus size={16} />新建</button>
          </div>
        </div>

        {error ? <div className="extensions-error" role="alert"><AlertTriangle size={16} />{error}<button onClick={() => void refresh()}>重试</button></div> : null}
        {loading ? <div className="extensions-loading">正在加载扩展…</div> : filtered.length === 0 ? (
          <div className="extensions-empty"><Icon size={30} /><strong>{meta.empty}</strong><span>{scope === "user" && kind === "mcp" ? "当前 MCP 配置暂按工作区管理。" : "可以通过导入或新建开始配置。"}</span></div>
        ) : (
          <div className={`extensions-list extensions-list-${kind}`}>
            {filtered.map((record) => <ExtensionRow key={record.id} kind={kind} record={record} onRemoveMcp={async (name) => { await removeMcpExtension(name); await refresh(); }} />)}
          </div>
        )}
      </main>
      <McpCreateDialog
        open={mcpDialogOpen}
        saving={mcpSaving}
        name={mcpName}
        command={mcpCommand}
        cwd={mcpCwd}
        onName={setMcpName}
        onCommand={setMcpCommand}
        onCwd={setMcpCwd}
        onClose={() => { if (!mcpSaving) setMcpDialogOpen(false); }}
        onSubmit={async () => {
          if (!mcpName.trim() || !mcpCommand.trim() || mcpSaving) return;
          setMcpSaving(true);
          setError("");
          try {
            await createMcpExtension({ name: mcpName.trim(), command: mcpCommand.trim().split(/\s+/u), cwd: mcpCwd.trim() || undefined });
            setMcpDialogOpen(false);
            setMcpName(""); setMcpCommand(""); setMcpCwd("");
            await refresh();
          } catch (reason) {
            setError(reason instanceof Error ? reason.message : "无法添加 MCP 服务");
          } finally { setMcpSaving(false); }
        }}
      />
    </div>
  );
}

function ExtensionRow({ kind, record, onRemoveMcp }: { kind: ExtensionKind; record: ExtensionRecord; onRemoveMcp: (name: string) => Promise<void> }) {
  if (kind === "plugins") return <PluginRow record={record as PluginExtensionRecord} />;
  if (kind === "mcp") return <McpRow record={record as McpExtensionRecord} onRemove={onRemoveMcp} />;
  return <SkillRow record={record as SkillExtensionRecord} />;
}

function PluginRow({ record }: { record: PluginExtensionRecord }) {
  return (
    <article className="extension-row">
      <div className="extension-row-icon plugin-icon"><Package size={21} /></div>
      <div className="extension-row-copy"><strong>{record.name}</strong><p>{record.description}</p><div className="extension-tags"><span>Skills {record.skills_count}</span><span>MCP {record.mcp_count}</span><span>Commands {record.commands_count}</span><small>v{record.version} · {record.source}</small></div></div>
      <span className="extension-status success"><CheckCircle2 size={14} />已启用</span>
      <button className="icon-button" aria-label={`删除插件 ${record.name}`} title="删除插件" disabled><Trash2 size={16} /></button>
    </article>
  );
}

function McpRow({ record, onRemove }: { record: McpExtensionRecord; onRemove: (name: string) => Promise<void> }) {
  const connected = record.status === "connected";
  const transport = typeof record.transport === "string" ? record.transport : "stdio";
  const [removing, setRemoving] = useState(false);
  return (
    <article className={`extension-row mcp-row ${connected ? "is-connected" : ""}`}>
      <div className="extension-row-icon mcp-icon">{transport === "stdio" ? <Terminal size={21} /> : <Globe2 size={21} />}</div>
      <div className="extension-row-copy"><strong>{record.name}</strong><p>{record.description} · {transport.toUpperCase()} · {record.tool_count} 个工具</p><div className="extension-detail-line"><span>{record.command || "未配置启动命令"}</span>{record.cwd ? <span>{record.cwd}</span> : null}{record.env_names?.length ? <span>环境变量 {record.env_names.length} 项（已脱敏）</span> : null}</div>{record.tools?.length ? <div className="extension-tool-list">{record.tools.map((tool) => <span key={tool}><Wrench size={11} />{tool}</span>)}</div> : null}</div>
      <span className={`extension-status ${connected ? "success" : "muted"}`}><span className="status-dot" />{connected ? "已连接" : "未连接"}</span>
      <button className="icon-button" aria-label={`移除 MCP ${record.name}`} title="移除 MCP" disabled={removing} onClick={() => { setRemoving(true); void onRemove(record.name).finally(() => setRemoving(false)); }}><Trash2 size={16} /></button>
    </article>
  );
}

function McpCreateDialog({ open, saving, name, command, cwd, onName, onCommand, onCwd, onClose, onSubmit }: { open: boolean; saving: boolean; name: string; command: string; cwd: string; onName: (value: string) => void; onCommand: (value: string) => void; onCwd: (value: string) => void; onClose: () => void; onSubmit: () => Promise<void> }) {
  if (!open) return null;
  return <div className="extensions-dialog-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}><form className="extensions-dialog" onSubmit={(event) => { event.preventDefault(); void onSubmit(); }}><header><div><strong>新建 MCP</strong><span>配置一个本地 stdio 工具服务</span></div><button type="button" className="icon-button" onClick={onClose} aria-label="关闭">×</button></header><label>服务名称<input value={name} onChange={(event) => onName(event.target.value)} placeholder="filesystem" autoFocus /></label><label>启动命令<input value={command} onChange={(event) => onCommand(event.target.value)} placeholder="npx -y @modelcontextprotocol/server-filesystem ." /></label><label>工作目录（可选）<input value={cwd} onChange={(event) => onCwd(event.target.value)} placeholder="D:\\code\\Agent_change" /></label><footer><button type="button" className="secondary-action" onClick={onClose} disabled={saving}>取消</button><button type="submit" className="primary-action" disabled={saving || !name.trim() || !command.trim()}>{saving ? "连接中…" : "连接并保存"}</button></footer></form></div>;
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
