import type { ChatMessage, ToolActivity } from "./types";

export function reconcileMessages(messages: ChatMessage[]): ChatMessage[] {
  const byKey = new Map<string, ChatMessage>();
  const order: string[] = [];
  for (const message of messages) {
    const key = logicalMessageKey(message);
    const existing = byKey.get(key);
    if (!existing) {
      byKey.set(key, message);
      order.push(key);
      continue;
    }
    byKey.set(key, mergeLogicalMessage(existing, message));
  }
  return order.map((key) => byKey.get(key)!);
}

export function composeTimeline(
  messages: ChatMessage[],
  notifications: ChatMessage[],
  hasTailWindow: boolean,
): ChatMessage[] {
  const persisted = reconcileMessages(messages.filter((message) => !isStandaloneNotification(message)));
  const visibleNotifications = notificationsForLoadedRanges(persisted, notifications, hasTailWindow);
  return reconcileMessages([...persisted, ...visibleNotifications]).sort(compareTimelineItems);
}

export function logicalMessageKey(message: ChatMessage): string {
  if (isStandaloneNotification(message)) return `notification:${message.id}`;
  if (message.turnId) return `${message.role}:${message.turnId}`;
  return `message:${message.id}`;
}

export function isStandaloneNotification(message: ChatMessage): boolean {
  return typeof message.seq !== "number"
    && (message.source === "scheduled_reminder" || message.source === "scheduled_soft");
}

function mergeLogicalMessage(current: ChatMessage, incoming: ChatMessage): ChatMessage {
  const currentPersisted = typeof current.seq === "number" && current.seq >= 0;
  const incomingPersisted = typeof incoming.seq === "number" && incoming.seq >= 0;
  const preferred = incomingPersisted && !currentPersisted ? incoming : current;
  const fallback = preferred === current ? incoming : current;
  const content = currentPersisted || incomingPersisted
    ? preferred.content || fallback.content
    : longestText(current.content, incoming.content);
  const thinking = currentPersisted || incomingPersisted
    ? preferred.thinking || fallback.thinking
    : longestText(current.thinking, incoming.thinking);
  return {
    ...fallback,
    ...preferred,
    content,
    thinking,
    media: preferred.media.length ? preferred.media : fallback.media,
    tools: mergeTools(preferred.tools, fallback.tools),
    streaming: incomingPersisted ? Boolean(incoming.streaming) : Boolean(preferred.streaming),
    durationMs: preferred.durationMs ?? fallback.durationMs,
  };
}

function longestText(current: string, incoming: string): string {
  return incoming.length > current.length ? incoming : current;
}

function mergeTools(preferred: ToolActivity[], fallback: ToolActivity[]): ToolActivity[] {
  const result = [...preferred];
  for (const tool of fallback) {
    if (!result.some((item) => item.callId === tool.callId)) result.push(tool);
  }
  return result;
}

function notificationsForLoadedRanges(
  messages: ChatMessage[],
  notifications: ChatMessage[],
  hasTailWindow: boolean,
): ChatMessage[] {
  const ranges = loadedMessageTimeRanges(messages);
  if (!ranges.length) return [];
  const tailRange = hasTailWindow ? ranges[ranges.length - 1] : null;
  return notifications.filter((message) => {
    if (!isStandaloneNotification(message)) return false;
    const time = Date.parse(message.timestamp || "");
    if (Number.isNaN(time)) return false;
    return ranges.some((range) => time >= range.startTime && time <= range.endTime)
      || Boolean(tailRange && time > tailRange.endTime);
  });
}

function loadedMessageTimeRanges(messages: ChatMessage[]): Array<{
  startSeq: number;
  endSeq: number;
  startTime: number;
  endTime: number;
}> {
  const ordered = messages
    .filter(hasPersistedSequence)
    .map((message) => ({ ...message, seq: Number(message.seq), time: Date.parse(message.timestamp || "") }))
    .filter((message) => !Number.isNaN(message.time))
    .sort((left, right) => left.seq - right.seq);
  const ranges: Array<{ startSeq: number; endSeq: number; startTime: number; endTime: number }> = [];
  for (const message of ordered) {
    const current = ranges[ranges.length - 1];
    if (!current || message.seq > current.endSeq + 1) {
      ranges.push({
        startSeq: message.seq,
        endSeq: message.seq,
        startTime: message.time,
        endTime: message.time,
      });
      continue;
    }
    current.endSeq = message.seq;
    current.startTime = Math.min(current.startTime, message.time);
    current.endTime = Math.max(current.endTime, message.time);
  }
  return ranges;
}

function compareTimelineItems(left: ChatMessage, right: ChatMessage): number {
  if (left.turnId && left.turnId === right.turnId && left.role !== right.role) {
    return left.role === "user" ? -1 : 1;
  }
  const leftRuntime = hasRuntimeSequence(left);
  const rightRuntime = hasRuntimeSequence(right);
  if (leftRuntime !== rightRuntime) return leftRuntime ? 1 : -1;
  if (leftRuntime && rightRuntime && left.seq !== right.seq) {
    return Number(left.seq) - Number(right.seq);
  }
  if (hasPersistedSequence(left) && hasPersistedSequence(right) && left.seq !== right.seq) {
    return left.seq - right.seq;
  }
  const leftTime = Date.parse(left.timestamp || "");
  const rightTime = Date.parse(right.timestamp || "");
  const leftMissing = Number.isNaN(leftTime);
  const rightMissing = Number.isNaN(rightTime);
  if (leftMissing && rightMissing) return 0;
  if (leftMissing) return 1;
  if (rightMissing) return -1;
  if (leftTime !== rightTime) return leftTime - rightTime;
  if (left.role !== right.role) return left.role === "user" ? -1 : 1;
  return logicalMessageKey(left).localeCompare(logicalMessageKey(right));
}

function hasPersistedSequence(message: ChatMessage): message is ChatMessage & { seq: number } {
  return typeof message.seq === "number" && message.seq >= 0;
}

function hasRuntimeSequence(message: ChatMessage): message is ChatMessage & { seq: number } {
  return typeof message.seq === "number" && message.seq < 0;
}
