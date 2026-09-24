import { ChevronDown, ChevronRight } from "lucide-react";
import type { ReactNode } from "react";

/** 只负责折叠入口，不包含新建、菜单等相邻操作，避免点击动作互相触发。 */
export function SidebarFoldToggle({ expanded, controls, label, collapsedSummary, className = "", onToggle, children }: {
  expanded: boolean;
  controls: string;
  label: string;
  collapsedSummary?: string;
  className?: string;
  onToggle: () => void;
  children: ReactNode;
}) {
  return (
    <button type="button" className={`sidebar-fold-toggle ${className}`}
      aria-label={label} aria-describedby={!expanded && collapsedSummary ? `${controls}-summary` : undefined}
      aria-expanded={expanded} aria-controls={controls} onClick={onToggle}>
      {expanded ? <ChevronDown size={12} aria-hidden="true" /> : <ChevronRight size={12} aria-hidden="true" />}
      {children}
      {!expanded && collapsedSummary ? <span className="sidebar-fold-count" id={`${controls}-summary`}>{collapsedSummary}</span> : null}
    </button>
  );
}
