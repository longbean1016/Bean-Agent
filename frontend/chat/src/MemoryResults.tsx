import { readMemoryResult, type MemoryHit } from "./turnPresentation";

export function parseMemoryResult(text: string): { count: number; items: MemoryHit[] } | null {
  try {
    return readMemoryResult(JSON.parse(text));
  } catch { return null; }
}

export function MemoryResults({ query, items, origin }: { query: string; items: MemoryHit[]; origin: string }) {
  return <div className="memory-results">
    <p className="memory-result-origin">{origin} · 检索命中不代表正文已使用</p>
    {query ? <p>查询：{query}</p> : null}
    {items.length ? <ul>{items.map((item, index) => <li key={`${item.id}:${index}`}>
      <p>{item.summary}</p>{item.id ? <small>记忆 ID：{item.id}</small> : null}
    </li>)}</ul> : <p>未找到相关记忆</p>}
  </div>;
}
