import type { ChatMessage } from "./types";

export type MemoryHit = { id: string; summary: string };
export type MemoryResult = { count: number; items: MemoryHit[] };
export type ProcessPart =
  | { id: string; kind: "thinking" | "text" | "answer"; text: string }
  | { id: string; kind: "tool"; call_id: string; memory_result?: MemoryResult }
  | { id: string; kind: "memory"; query: string; items: MemoryHit[]; duration_ms?: number };
export type TurnPresentation = { version: 1; started_at?: string; final?: boolean; parts: ProcessPart[] };

/** 展示元数据向后兼容；未知版本不影响原有正文与工具记录。 */
export function readPresentation(value: unknown): TurnPresentation | undefined {
  if (!value || typeof value !== "object") return undefined;
  const data = value as Record<string, unknown>;
  if (data.version !== 1 || !Array.isArray(data.parts)) return undefined;
  const parts: ProcessPart[] = [];
  const ids = new Set<string>();
  for (const raw of data.parts) {
    if (!raw || typeof raw !== "object") continue;
    const part = raw as Record<string, unknown>;
    if (typeof part.id !== "string" || ids.has(part.id)) continue;
    if ((part.kind === "thinking" || part.kind === "text" || part.kind === "answer") && typeof part.text === "string") {
      parts.push({ id: part.id, kind: part.kind, text: part.text });
    } else if (part.kind === "tool" && typeof part.call_id === "string") {
      const memory = readMemoryResult(part.memory_result);
      parts.push({ id: part.id, kind: "tool", call_id: part.call_id, ...(memory ? { memory_result: memory } : {}) });
    } else if (part.kind === "memory" && Array.isArray(part.items)) {
      const items = readMemoryHits(part.items);
      if (items.length) parts.push({ id: part.id, kind: "memory", query: String(part.query ?? ""), items,
        ...(typeof part.duration_ms === "number" && part.duration_ms >= 0 ? { duration_ms: part.duration_ms } : {}) });
    }
    ids.add(part.id);
  }
  return { version: 1, parts, final: data.final === true,
    ...(typeof data.started_at === "string" ? { started_at: data.started_at } : {}) };
}

export function readMemoryHits(items: unknown[]): MemoryHit[] {
  return items.flatMap((item) => {
    if (!item || typeof item !== "object") return [];
    const record = item as Record<string, unknown>;
    return typeof record.summary === "string" ? [{ id: String(record.id ?? ""), summary: record.summary }] : [];
  });
}

export function readMemoryResult(value: unknown): MemoryResult | null {
  if (!value || typeof value !== "object") return null;
  const result = value as Record<string, unknown>;
  if (!Array.isArray(result.items)) return null;
  const items = readMemoryHits(result.items);
  return { count: typeof result.count === "number" && result.count >= 0 ? result.count : items.length, items };
}

export function appendProcessDelta(presentation: TurnPresentation | undefined, id: string | undefined,
  kind: "thinking" | "text", delta: string): TurnPresentation | undefined {
  if (!id) return presentation;
  const base = presentation ?? { version: 1 as const, parts: [] };
  const index = base.parts.findIndex((part) => part.id === id);
  const parts = [...base.parts];
  const previous = parts[index];
  if (previous && previous.kind === kind) parts[index] = { ...previous, text: previous.text + delta };
  else if (index < 0) parts.push({ id, kind, text: delta });
  return { ...base, parts };
}

export function messageProcess(message: ChatMessage): { parts: ProcessPart[]; body: string } {
  const presentation = message.presentation;
  if (!presentation) {
    // 旧消息没有逐段时序，只保留原来的思考与工具顺序，不推测思考的分段位置。
    return { parts: [
      ...(message.thinking ? [{ id: "legacy-thinking", kind: "thinking" as const, text: message.thinking }] : []),
      ...message.tools.map((tool) => ({ id: `tool-${tool.callId}`, kind: "tool" as const, call_id: tool.callId })),
    ], body: message.content };
  }
  const parts = presentation.parts.filter((part) => part.kind !== "answer");
  if (presentation.final) return { parts, body: message.streaming
    ? presentation.parts.flatMap((part) => part.kind === "answer" ? [part.text] : []).join("")
    : message.content };
  // 尚未获得结束原因时，最后一段文字照常实时显示；后续工具到达后自然成为过程文字。
  const last = parts.at(-1);
  if ((message.streaming || message.status === "interrupted") && last?.kind === "text") {
    return { parts: parts.slice(0, -1), body: last.text };
  }
  return { parts, body: message.streaming || message.status === "interrupted" ? "" : message.content };
}
