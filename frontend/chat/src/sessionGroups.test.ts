import { expect, it } from "vitest";

import { groupSessionsByCreatedAt } from "./sessionGroups";
import type { SessionSummary } from "./types";

function session(key: string, createdAt: string): SessionSummary {
  return { key, created_at: createdAt, updated_at: createdAt, message_count: 1, first_message_content: key };
}

it("按本地自然日将会话分入互斥时间区间", () => {
  const groups = groupSessionsByCreatedAt([
    session("month", "2026-05-10T12:00:00+08:00"),
    session("thirty", "2026-06-25T12:00:00+08:00"),
    session("week", "2026-07-15T12:00:00+08:00"),
    session("yesterday", "2026-07-18T12:00:00+08:00"),
    session("today", "2026-07-19T09:00:00+08:00"),
  ], new Date("2026-07-19T18:00:00+08:00"));

  expect(groups.map((group) => [group.label, group.sessions.map((item) => item.key)])).toEqual([
    ["今天", ["today"]],
    ["昨天", ["yesterday"]],
    ["7天内", ["week"]],
    ["30天内", ["thirty"]],
    ["2026-05", ["month"]],
  ]);
});

it("隐藏空分组并在组内按创建时间倒序", () => {
  const groups = groupSessionsByCreatedAt([
    session("early", "2026-07-19T08:00:00+08:00"),
    session("late", "2026-07-19T12:00:00+08:00"),
  ], new Date("2026-07-19T18:00:00+08:00"));

  expect(groups).toHaveLength(1);
  expect(groups[0].label).toBe("今天");
  expect(groups[0].sessions.map((item) => item.key)).toEqual(["late", "early"]);
});
