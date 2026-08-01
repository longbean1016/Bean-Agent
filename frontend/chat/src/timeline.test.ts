import { describe, expect, it } from "vitest";

import { composeTimeline, reconcileMessages } from "./timeline";
import type { ChatMessage } from "./types";

function message(overrides: Partial<ChatMessage>): ChatMessage {
  return {
    id: "message",
    role: "assistant",
    content: "",
    thinking: "",
    media: [],
    tools: [],
    ...overrides,
  };
}

describe("timeline", () => {
  it("replaces a running draft with the persisted assistant from the same turn", () => {
    const result = reconcileMessages([
      message({ id: "turn-1", turnId: "turn-1", content: "partial", streaming: true }),
      message({ id: "web:one:2", seq: 2, turnId: "turn-1", content: "final", streaming: false }),
    ]);

    expect(result).toHaveLength(1);
    expect(result[0]).toMatchObject({
      id: "web:one:2",
      seq: 2,
      turnId: "turn-1",
      content: "final",
      streaming: false,
    });
  });

  it("keeps the user before the assistant when a turn has the same timestamp", () => {
    const timestamp = "2026-07-31T23:00:00+08:00";
    const result = composeTimeline([
      message({ id: "assistant-row", seq: 2, turnId: "turn-1", timestamp }),
      message({ id: "user-row", seq: 1, role: "user", turnId: "turn-1", timestamp }),
    ], [], true);

    expect(result.map((item) => item.role)).toEqual(["user", "assistant"]);
    expect(result.map((item) => item.id)).toEqual(["user-row", "assistant-row"]);
  });

  it("keeps a running snapshot after the persisted message window", () => {
    const result = composeTimeline([
      message({
        id: "persisted-user",
        seq: 40,
        role: "user",
        turnId: "persisted-turn",
        timestamp: "2026-08-01T09:00:00+08:00",
      }),
      message({
        id: "persisted-assistant",
        seq: 41,
        turnId: "persisted-turn",
        timestamp: "2026-08-01T09:01:00+08:00",
      }),
      message({
        id: "running:user:active-turn",
        seq: -2,
        role: "user",
        turnId: "active-turn",
        timestamp: "2026-08-01T10:00:00+08:00",
      }),
      message({
        id: "running:assistant:active-turn",
        seq: -1,
        turnId: "active-turn",
        streaming: true,
        timestamp: "2026-08-01T10:00:00+08:00",
      }),
    ], [], true);

    expect(result.map((item) => item.id)).toEqual([
      "persisted-user",
      "persisted-assistant",
      "running:user:active-turn",
      "running:assistant:active-turn",
    ]);
  });

  it("derives reminders without adding them to persisted messages", () => {
    const persisted = [
      message({ id: "m1", seq: 1, role: "user", timestamp: "2026-07-31T08:00:00+08:00" }),
      message({ id: "m2", seq: 2, timestamp: "2026-07-31T10:00:00+08:00" }),
    ];
    const notifications = [message({
      id: "n1",
      source: "scheduled_reminder",
      content: "drink water",
      timestamp: "2026-07-31T09:00:00+08:00",
    })];

    const timeline = composeTimeline(persisted, notifications, true);

    expect(timeline.map((item) => item.id)).toEqual(["m1", "n1", "m2"]);
    expect(persisted.map((item) => item.id)).toEqual(["m1", "m2"]);
  });

  it("does not render reminders from gaps between disjoint loaded ranges", () => {
    const persisted = [
      message({ id: "m1", seq: 1, timestamp: "2026-07-31T08:00:00+08:00" }),
      message({ id: "m2", seq: 2, timestamp: "2026-07-31T08:01:00+08:00" }),
      message({ id: "m9", seq: 9, timestamp: "2026-07-31T12:00:00+08:00" }),
      message({ id: "m10", seq: 10, timestamp: "2026-07-31T12:01:00+08:00" }),
    ];
    const notifications = [
      message({ id: "old", source: "scheduled_reminder", timestamp: "2026-07-31T08:00:30+08:00" }),
      message({ id: "gap", source: "scheduled_reminder", timestamp: "2026-07-31T10:00:00+08:00" }),
      message({ id: "tail", source: "scheduled_reminder", timestamp: "2026-07-31T12:30:00+08:00" }),
    ];

    expect(composeTimeline(persisted, notifications, true).map((item) => item.id)).toEqual([
      "m1", "old", "m2", "m9", "m10", "tail",
    ]);
  });
});
