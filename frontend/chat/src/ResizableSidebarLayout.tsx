import { PanelLeftClose, PanelLeftOpen } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import type { CSSProperties, PointerEvent as ReactPointerEvent, ReactNode } from "react";

const DEFAULT_SIDEBAR_WIDTH = 272;
const MIN_SIDEBAR_WIDTH = 220;
const MAX_SIDEBAR_WIDTH = 420;
const SIDEBAR_LAYOUT_STORAGE_KEY = "beanagent.desktop_sidebar_layout";

type SavedSidebarLayout = {
  width?: number;
  collapsed?: boolean;
};

function clampSidebarWidth(width: number): number {
  return Math.min(MAX_SIDEBAR_WIDTH, Math.max(MIN_SIDEBAR_WIDTH, Math.round(width)));
}

function readSavedLayout(): Required<SavedSidebarLayout> {
  try {
    const saved = JSON.parse(window.localStorage.getItem(SIDEBAR_LAYOUT_STORAGE_KEY) || "{}") as SavedSidebarLayout;
    return {
      width: clampSidebarWidth(Number(saved.width) || DEFAULT_SIDEBAR_WIDTH),
      collapsed: saved.collapsed === true,
    };
  } catch {
    return { width: DEFAULT_SIDEBAR_WIDTH, collapsed: false };
  }
}

export function ResizableSidebarLayout(props: {
  sidebar: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  const initialLayout = useRef(readSavedLayout());
  const [sidebarWidth, setSidebarWidth] = useState(initialLayout.current.width);
  const [collapsed, setCollapsed] = useState(initialLayout.current.collapsed);
  const [resizing, setResizing] = useState(false);
  const dragStateRef = useRef<{ pointerId: number; startX: number; startWidth: number } | null>(null);
  const collapseButtonRef = useRef<HTMLButtonElement>(null);
  const expandButtonRef = useRef<HTMLButtonElement>(null);
  const pendingFocusRef = useRef<"collapse" | "expand" | null>(null);

  useEffect(() => {
    try {
      window.localStorage.setItem(SIDEBAR_LAYOUT_STORAGE_KEY, JSON.stringify({ width: sidebarWidth, collapsed }));
    } catch {
      // 浏览器禁用存储时仍保留当前会话内的布局状态。
    }
  }, [collapsed, sidebarWidth]);

  useEffect(() => {
    if (pendingFocusRef.current === "expand" && collapsed) expandButtonRef.current?.focus();
    if (pendingFocusRef.current === "collapse" && !collapsed) collapseButtonRef.current?.focus();
    pendingFocusRef.current = null;
  }, [collapsed]);

  const updateWidth = (width: number) => setSidebarWidth(clampSidebarWidth(width));
  const finishResize = (event: ReactPointerEvent<HTMLButtonElement>) => {
    if (!dragStateRef.current || dragStateRef.current.pointerId !== event.pointerId) return;
    dragStateRef.current = null;
    setResizing(false);
    if (event.currentTarget.hasPointerCapture?.(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
  };

  const layoutStyle = { "--desktop-sidebar-width": `${sidebarWidth}px` } as CSSProperties;
  const layoutClassName = [
    "app-shell",
    "resizable-sidebar-layout",
    props.className,
    collapsed ? "sidebar-collapsed" : "",
    resizing ? "sidebar-resizing" : "",
  ].filter(Boolean).join(" ");

  return (
    <div className={layoutClassName} style={layoutStyle}>
      <aside className="desktop-sidebar resizable-desktop-sidebar" aria-hidden={collapsed} {...(collapsed ? { inert: true } : {})}>
        {props.sidebar}
        <button
          ref={collapseButtonRef}
          type="button"
          className="icon-button desktop-sidebar-collapse"
          aria-label="收起左侧栏"
          title="收起左侧栏"
          onClick={() => {
            pendingFocusRef.current = "expand";
            setCollapsed(true);
          }}
        ><PanelLeftClose size={17} /></button>
      </aside>

      <button
        type="button"
        className="desktop-sidebar-resizer"
        role="separator"
        aria-label={`调整左侧栏宽度，当前 ${sidebarWidth} 像素`}
        aria-orientation="vertical"
        aria-valuemin={MIN_SIDEBAR_WIDTH}
        aria-valuemax={MAX_SIDEBAR_WIDTH}
        aria-valuenow={sidebarWidth}
        onDoubleClick={() => updateWidth(DEFAULT_SIDEBAR_WIDTH)}
        onKeyDown={(event) => {
          if (event.key === "ArrowLeft") updateWidth(sidebarWidth - 8);
          else if (event.key === "ArrowRight") updateWidth(sidebarWidth + 8);
          else if (event.key === "Home") updateWidth(MIN_SIDEBAR_WIDTH);
          else if (event.key === "End") updateWidth(MAX_SIDEBAR_WIDTH);
          else return;
          event.preventDefault();
        }}
        onPointerDown={(event) => {
          dragStateRef.current = { pointerId: event.pointerId, startX: event.clientX, startWidth: sidebarWidth };
          setResizing(true);
          event.currentTarget.setPointerCapture?.(event.pointerId);
          event.preventDefault();
        }}
        onPointerMove={(event) => {
          const dragState = dragStateRef.current;
          if (!dragState || dragState.pointerId !== event.pointerId) return;
          updateWidth(dragState.startWidth + event.clientX - dragState.startX);
        }}
        onPointerUp={finishResize}
        onPointerCancel={finishResize}
      ><span aria-hidden="true" /></button>

      {collapsed ? <button
        ref={expandButtonRef}
        type="button"
        className="icon-button desktop-sidebar-expand"
        aria-label="展开左侧栏"
        title="展开左侧栏"
        onClick={() => {
          pendingFocusRef.current = "collapse";
          setCollapsed(false);
        }}
      ><PanelLeftOpen size={18} /></button> : null}

      {props.children}
    </div>
  );
}
