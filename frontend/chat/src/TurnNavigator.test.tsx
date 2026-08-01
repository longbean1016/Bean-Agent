import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import {
  activeTurnFromVisibleRegions,
  groupMessagesIntoNavigationTurns,
  messagesWithNavigationTurns,
  scrollTopToRevealItem,
  TurnNavigator,
  turnsFromMessages,
} from "./TurnNavigator";
import type { ChatMessage } from "./types";

function message(id: string, role: "user" | "assistant", content: string): ChatMessage {
  return { id, role, content, thinking: "", media: [], tools: [], turnId: id };
}

describe("TurnNavigator", () => {
  it("keeps the current turn active while any part of its assistant response remains visible", () => {
    const turns = [
      { id: "u1", question: "第一问", preview: "第一问" },
      { id: "u2", question: "第二问", preview: "第二问" },
      { id: "u3", question: "第三问", preview: "第三问" },
    ];

    expect(activeTurnFromVisibleRegions(turns, new Map([
      ["u1", [{ top: -300, bottom: 80 }]],
      ["u2", [{ top: 100, bottom: 420 }]],
      ["u3", [{ top: 450, bottom: 700 }]],
    ]), 0, 600, false, "down")).toBe("u1");
    expect(activeTurnFromVisibleRegions(turns, new Map([
      ["u1", [{ top: -300, bottom: 80 }]],
      ["u2", [{ top: 100, bottom: 420 }]],
      ["u3", [{ top: 450, bottom: 700 }]],
    ]), 0, 600, false, "up")).toBe("u3");
    expect(activeTurnFromVisibleRegions(turns, new Map([
      ["u1", [{ top: -340, bottom: -1 }]],
      ["u2", [{ top: 1, bottom: 380 }]],
      ["u3", [{ top: 410, bottom: 680 }]],
    ]), 0, 600)).toBe("u2");
    expect(activeTurnFromVisibleRegions(turns, new Map([
      ["u1", [{ top: -900, bottom: -500 }]],
      ["u2", [{ top: -360, bottom: -20 }]],
      ["u3", [{ top: 280, bottom: 500 }]],
    ]), 0, 600, true)).toBe("u3");
  });

  it("keeps the previous turn active while layout changes leave no visible region", () => {
    const turns = [
      { id: "u1", question: "第一问", preview: "第一问" },
      { id: "u2", question: "第二问", preview: "第二问" },
      { id: "u3", question: "第三问", preview: "第三问" },
    ];

    expect(activeTurnFromVisibleRegions(
      turns,
      new Map(),
      0,
      600,
      false,
      "down",
      "u3",
    )).toBe("u3");
  });

  it("keeps the active turn while its region remains visible", () => {
    const turns = [
      { id: "u1", question: "第一问", preview: "第一问" },
      { id: "u2", question: "第二问", preview: "第二问" },
      { id: "u3", question: "第三问", preview: "第三问" },
    ];
    const regions = new Map([
      ["u1", [{ top: -300, bottom: 80 }]],
      ["u2", [{ top: 100, bottom: 420 }]],
      ["u3", [{ top: 450, bottom: 700 }]],
    ]);

    expect(activeTurnFromVisibleRegions(
      turns,
      regions,
      0,
      600,
      false,
      "up",
      "u2",
    )).toBe("u2");
  });

  it("assigns each assistant response to the preceding user turn", () => {
    expect(messagesWithNavigationTurns([
      message("u1", "user", "第一问"),
      message("a1", "assistant", "第一答"),
      message("u2", "user", "第二问"),
      message("a2", "assistant", "第二答"),
    ]).map((item) => item.navigationTurnId)).toEqual(["u1", "u1", "u2", "u2"]);
  });

  it("assigns a proactive message to the nearest preceding user turn", () => {
    const proactive = {
      ...message("p1", "assistant", "proactive follow-up"),
      proactive: true,
      turnId: "",
    };

    expect(messagesWithNavigationTurns([
      message("u1", "user", "first question"),
      message("a1", "assistant", "first answer"),
      proactive,
    ]).map((item) => item.navigationTurnId)).toEqual(["u1", "u1", "u1"]);

    expect(groupMessagesIntoNavigationTurns([
      message("u1", "user", "first question"),
      message("a1", "assistant", "first answer"),
      proactive,
    ])).toMatchObject([{
      navigationTurnId: "u1",
      messages: [{ id: "u1" }, { id: "a1" }, { id: "p1" }],
    }]);
  });

  it("does not invent a navigation turn for proactive-only history", () => {
    const proactive = {
      ...message("p1", "assistant", "proactive follow-up"),
      proactive: true,
      turnId: "",
    };

    expect(messagesWithNavigationTurns([proactive])[0].navigationTurnId).toBe("");
    expect(turnsFromMessages([proactive])).toEqual([]);
  });

  it("groups each user message and its assistant response into one render section", () => {
    const groups = groupMessagesIntoNavigationTurns([
      message("u1", "user", "第一问"),
      message("a1", "assistant", "第一答"),
      message("u2", "user", "第二问"),
      message("a2", "assistant", "第二答"),
    ]);

    expect(groups.map((group) => ({
      id: group.navigationTurnId,
      messages: group.messages.map((item) => item.id),
    }))).toEqual([
      { id: "u1", messages: ["u1", "a1"] },
      { id: "u2", messages: ["u2", "a2"] },
    ]);
  });

  it("centers an active navigation item only after it leaves its rail", () => {
    expect(scrollTopToRevealItem(0, 0, 100, 220, 240)).toBe(180);
    expect(scrollTopToRevealItem(180, 100, 220, 80, 100)).toBe(110);
    expect(scrollTopToRevealItem(100, 100, 220, 140, 160)).toBe(100);
  });

  it("builds one entry per user turn and truncates long questions", () => {
    const turns = turnsFromMessages([
      message("u1", "user", "第一个问题"),
      message("a1", "assistant", "第一个回答"),
      message("u2", "user", "这是一个需要被截断展示的非常非常长的第二个问题"),
    ], 16);

    expect(turns).toEqual([
      { id: "u1", question: "第一个问题", preview: "第一个问题" },
      { id: "u2", question: "这是一个需要被截断展示的非常非常长的第二个问题", preview: "这是一个需要被截断展示的非常非常…" },
    ]);
  });

  it("renders the full question directory and scrolls to a selected turn", () => {
    const scrollTo = vi.fn();
    const onTurnRequest = vi.fn();
    const turns = [
      { id: "u1", question: "第一个问题", preview: "第一个问题" },
      { id: "u2", question: "第二个问题", preview: "第二个问题" },
    ];
    const { container } = render(
      <div className="conversation">
        <div className="conversation-scroll">
          <section data-turn-region="u1"><article data-turn-anchor="u1" /></section>
          <section data-turn-region="u2"><article data-turn-anchor="u2" /></section>
        </div>
        <TurnNavigator sessionId="web:test" turns={turns} onTurnRequest={onTurnRequest} />
      </div>,
    );
    const scroller = container.querySelector<HTMLElement>(".conversation-scroll")!;
    const anchors = Array.from(container.querySelectorAll<HTMLElement>("[data-turn-anchor]"));
    const regions = Array.from(container.querySelectorAll<HTMLElement>("[data-turn-region]"));
    Object.defineProperties(scroller, {
      clientHeight: { configurable: true, value: 600 },
      scrollHeight: { configurable: true, value: 1200 },
      scrollTop: { configurable: true, writable: true, value: 0 },
      scrollTo: { configurable: true, value: scrollTo },
    });
    scroller.getBoundingClientRect = () => ({ top: 0, bottom: 600, left: 0, right: 800, width: 800, height: 600, x: 0, y: 0, toJSON: () => ({}) });
    anchors[0].getBoundingClientRect = () => ({ top: 40, bottom: 80, left: 0, right: 0, width: 0, height: 40, x: 0, y: 40, toJSON: () => ({}) });
    anchors[1].getBoundingClientRect = () => ({ top: 640, bottom: 680, left: 0, right: 0, width: 0, height: 40, x: 0, y: 640, toJSON: () => ({}) });
    regions[0].getBoundingClientRect = () => ({ top: -400, bottom: -100, left: 0, right: 800, width: 800, height: 300, x: 0, y: -400, toJSON: () => ({}) });
    regions[1].getBoundingClientRect = () => ({ top: 0, bottom: 500, left: 0, right: 800, width: 800, height: 500, x: 0, y: 0, toJSON: () => ({}) });

    expect(screen.getAllByRole("button", { name: /跳转到/ })).toHaveLength(2);
    expect(screen.getByRole("button", { name: "02第二个问题" })).toHaveAttribute("aria-current", "true");

    fireEvent.click(screen.getByRole("button", { name: "01第一个问题" }));
    expect(onTurnRequest).toHaveBeenCalledWith(turns[0]);
    expect(scrollTo).not.toHaveBeenCalled();
    const firstTurnButton = screen.getByRole("button", { name: "01第一个问题" });
    expect(firstTurnButton).toHaveAttribute("aria-current", "true");

    scroller.scrollTop = 100;
    fireEvent.scroll(scroller);
    expect(firstTurnButton).toHaveAttribute("aria-current", "true");
  });
});
