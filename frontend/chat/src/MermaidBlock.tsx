import { mermaid } from "@streamdown/mermaid";
import { ChartNoAxesCombined, Check, Code2, Copy, Download, Maximize2, RotateCcw, X, ZoomIn, ZoomOut } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type { CustomRendererProps } from "streamdown";

type ViewMode = "diagram" | "code";

export function MermaidBlock({ code, isIncomplete }: CustomRendererProps) {
  const [mode, setMode] = useState<ViewMode>("diagram");
  const [svg, setSvg] = useState("");
  const [error, setError] = useState("");
  const [retryKey, setRetryKey] = useState(0);
  const [copied, setCopied] = useState(false);
  const [fullscreen, setFullscreen] = useState(false);

  useEffect(() => {
    if (isIncomplete) return;
    let cancelled = false;
    setSvg("");
    setError("");
    void mermaid.getMermaid().render(`beanagent-mermaid-${crypto.randomUUID()}`, code)
      .then((result) => { if (!cancelled) setSvg(result.svg); })
      .catch((reason: unknown) => {
        if (!cancelled) setError(reason instanceof Error ? reason.message : "无法解析 Mermaid 源码");
      });
    return () => { cancelled = true; };
  }, [code, isIncomplete, retryKey]);

  useEffect(() => {
    if (!fullscreen) return;
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const closeOnEscape = (event: KeyboardEvent) => { if (event.key === "Escape") setFullscreen(false); };
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.body.style.overflow = previous;
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [fullscreen]);

  const copySource = async () => {
    await navigator.clipboard.writeText(code);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1600);
  };

  const downloadCurrent = () => {
    const diagram = mode === "diagram" && svg;
    const blob = new Blob([diagram || code], { type: diagram ? "image/svg+xml" : "text/plain" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = diagram ? "diagram.svg" : "diagram.mmd";
    link.click();
    URL.revokeObjectURL(url);
  };

  return (
    <div className="mermaid-viewer" data-streamdown="mermaid-block">
      <div className="mermaid-toolbar">
        <div className="mermaid-tabs" role="group" aria-label="流程图视图">
          <button type="button" aria-pressed={mode === "diagram"} onClick={() => setMode("diagram")}><ChartNoAxesCombined size={15} />图表</button>
          <button type="button" aria-pressed={mode === "code"} onClick={() => setMode("code")}><Code2 size={15} />代码</button>
        </div>
        <div className="mermaid-actions">
          <button type="button" title="复制 Mermaid 源码" onClick={() => void copySource()}>{copied ? <Check size={16} /> : <Copy size={16} />}<span>{copied ? "已复制" : "复制"}</span></button>
          <button type="button" title={mode === "diagram" ? "下载 SVG" : "下载 Mermaid 源码"} onClick={downloadCurrent}><Download size={16} /><span>下载</span></button>
          <button type="button" title="全屏查看" disabled={mode !== "diagram" || !svg} onClick={() => setFullscreen(true)}><Maximize2 size={16} /><span>全屏</span></button>
        </div>
      </div>
      {mode === "code" ? <pre className="mermaid-source"><code>{code}</code></pre> : (
        <div className="mermaid-preview" data-streamdown="mermaid">
          {error ? (
            <div className="mermaid-error" role="alert"><strong>流程图生成失败</strong><p>{error}</p><button type="button" onClick={() => setRetryKey((key) => key + 1)}><RotateCcw size={14} />重试</button></div>
          ) : svg ? <MermaidCanvas svg={svg} interactive={false} /> : <div className="mermaid-loading">正在生成流程图</div>}
        </div>
      )}
      {fullscreen ? createPortal(<MermaidFullscreen svg={svg} onClose={() => setFullscreen(false)} />, document.body) : null}
    </div>
  );
}

function MermaidCanvas({ svg, interactive }: { svg: string; interactive: boolean }) {
  const [zoom, setZoom] = useState(1);
  const [offset, setOffset] = useState({ x: 0, y: 0 });
  const dragRef = useRef<{ x: number; y: number; originX: number; originY: number } | null>(null);
  const reset = () => { setZoom(1); setOffset({ x: 0, y: 0 }); };

  return (
    <div className={`mermaid-canvas ${interactive ? "interactive" : "static"}`}>
      {interactive ? <div className="mermaid-zoom-controls">
        <button type="button" title="放大" onClick={() => setZoom((value) => Math.min(3, value + .15))}><ZoomIn size={17} /></button>
        <button type="button" title="缩小" onClick={() => setZoom((value) => Math.max(.35, value - .15))}><ZoomOut size={17} /></button>
        <button type="button" title="复位视图" onClick={reset}><RotateCcw size={17} /></button>
      </div> : null}
      <div
        className="mermaid-svg"
        role="img"
        aria-label="Mermaid 流程图"
        onWheel={interactive ? (event) => { event.preventDefault(); setZoom((value) => Math.max(.35, Math.min(3, value + (event.deltaY < 0 ? .1 : -.1)))); } : undefined}
        onPointerDown={interactive ? (event) => { dragRef.current = { x: event.clientX, y: event.clientY, originX: offset.x, originY: offset.y }; event.currentTarget.setPointerCapture(event.pointerId); } : undefined}
        onPointerMove={interactive ? (event) => { const drag = dragRef.current; if (drag) setOffset({ x: drag.originX + event.clientX - drag.x, y: drag.originY + event.clientY - drag.y }); } : undefined}
        onPointerUp={interactive ? () => { dragRef.current = null; } : undefined}
        style={interactive ? { transform: `translate(${offset.x}px, ${offset.y}px) scale(${zoom})` } : undefined}
        dangerouslySetInnerHTML={{ __html: svg }}
      />
    </div>
  );
}

function MermaidFullscreen({ svg, onClose }: { svg: string; onClose: () => void }) {
  return (
    <div className="mermaid-fullscreen" role="dialog" aria-modal="true" aria-label="流程图大图">
      <button className="mermaid-fullscreen-close" type="button" title="关闭大图" onClick={onClose}><X size={20} /></button>
      <MermaidCanvas svg={svg} interactive />
    </div>
  );
}
