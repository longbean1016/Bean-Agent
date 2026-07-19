import type { SessionSummary } from "./types";

export interface SessionGroup {
  label: string;
  sessions: SessionSummary[];
}

const DAY_MS = 24 * 60 * 60 * 1000;

function calendarDay(date: Date): number {
  return Date.UTC(date.getFullYear(), date.getMonth(), date.getDate()) / DAY_MS;
}

function monthLabel(date: Date): string {
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}`;
}

export function groupSessionsByUpdatedAt(sessions: SessionSummary[], now = new Date()): SessionGroup[] {
  const sorted = [...sessions].sort((left, right) => Date.parse(right.updated_at) - Date.parse(left.updated_at));
  const buckets = new Map<string, SessionSummary[]>();
  const today = calendarDay(now);

  for (const session of sorted) {
    const updated = new Date(session.updated_at);
    const age = Number.isNaN(updated.getTime()) ? 30 : Math.max(0, today - calendarDay(updated));
    const label = age === 0
      ? "今天"
      : age === 1
        ? "昨天"
        : age < 7
          ? "7天内"
          : age < 30
            ? "30天内"
            : Number.isNaN(updated.getTime()) ? "更早" : monthLabel(updated);
    const bucket = buckets.get(label) ?? [];
    bucket.push(session);
    buckets.set(label, bucket);
  }

  const recent = ["今天", "昨天", "7天内", "30天内"]
    .filter((label) => buckets.has(label))
    .map((label) => ({ label, sessions: buckets.get(label)! }));
  const archive = [...buckets.entries()]
    .filter(([label]) => !["今天", "昨天", "7天内", "30天内"].includes(label))
    .sort(([left], [right]) => right.localeCompare(left))
    .map(([label, grouped]) => ({ label, sessions: grouped }));
  return [...recent, ...archive];
}
