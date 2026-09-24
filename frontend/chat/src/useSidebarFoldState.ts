import { useEffect, useState } from "react";

export const SIDEBAR_FOLD_STORAGE_KEY = "beanagent.sidebar_collapsed_groups.v1";

function isFoldKey(value: unknown): value is string {
  return typeof value === "string" && (
    ["pinned", "projects", "recent"].includes(value)
    || (value.startsWith("workspace:") && value.length > "workspace:".length)
  );
}

function readCollapsedGroups(): Set<string> {
  try {
    const saved: unknown = JSON.parse(window.localStorage.getItem(SIDEBAR_FOLD_STORAGE_KEY) || "[]");
    return new Set(Array.isArray(saved) ? saved.filter(isFoldKey) : []);
  } catch {
    // 存储不可用或旧数据损坏时回退为展开，不影响侧栏操作。
    return new Set();
  }
}

/** 只保存展示偏好；目录按稳定 ID 记录，不依赖名称或异步加载的列表。 */
export function useSidebarFoldState() {
  const [collapsedGroups, setCollapsedGroups] = useState(readCollapsedGroups);

  useEffect(() => {
    try {
      window.localStorage.setItem(SIDEBAR_FOLD_STORAGE_KEY, JSON.stringify([...collapsedGroups]));
    } catch {
      // 浏览器禁用存储或配额不足时，当前页面仍可正常展开和收起。
    }
  }, [collapsedGroups]);

  return [collapsedGroups, setCollapsedGroups] as const;
}
