import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import {
  activeTurnAtViewportTop,
  scrollTopToRevealItem,
  TurnNavigator,
  turnsFromMessages,
} from "./TurnNavigator";
import type { ChatMessage } from "./types";

function message(id: string, role: "user" | "assistant", content: string): ChatMessage {
  return { id, role, content, thinking: "", media: [], tools: [], turnId: id };
}

describe("TurnNavigator", () => {
  it("keeps the current turn active until the next user anchor crosses the viewport top", () => {
    const turns = [
      { id: "u1", question: "第一问", preview: "第一问" },
      { id: "u2", question: "第二问", preview: "第二问" },
      { id: "u3", question: "第三问", preview: "第三问" },
    ];

    expect(activeTurnAtViewportTop(turns, new Map([
      ["u1", -500],
      ["u2", 1],
      ["u3", 700],
    ]), 0)).toBe("u1");
    expect(activeTurnAtViewportTop(turns, new Map([
      ["u1", -540],
      ["u2", -1],
      ["u3", 660],
    ]), 0)).toBe("u2");
    expect(activeTurnAtViewportTop(turns, new Map([
      ["u1", -900],
      ["u2", -360],
      ["u3", 280],
    ]), 0, true)).toBe("u3");
  });

  it("calculates the smallest internal scroll needed to reveal an active navigation item", () => {
    expect(scrollTopToRevealItem(0, 120, 240, 20)).toBe(140);
    expect(scrollTopToRevealItem(180, 120, 80, 20)).toBe(80);
    expect(scrollTopToRevealItem(100, 120, 140, 20)).toBe(100);
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
    const turns = [
      { id: "u1", question: "第一个问题", preview: "第一个问题" },
      { id: "u2", question: "第二个问题", preview: "第二个问题" },
    ];
    const { container } = render(
      <div className="conversation">
        <div className="conversation-scroll">
          <article data-turn-anchor="u1" />
          <article data-turn-anchor="u2" />
        </div>
        <TurnNavigator sessionId="web:test" turns={turns} />
      </div>,
    );
    const scroller = container.querySelector<HTMLElement>(".conversation-scroll")!;
    const anchors = Array.from(container.querySelectorAll<HTMLElement>("[data-turn-anchor]"));
    Object.defineProperties(scroller, {
      clientHeight: { configurable: true, value: 600 },
      scrollHeight: { configurable: true, value: 1200 },
      scrollTop: { configurable: true, writable: true, value: 0 },
      scrollTo: { configurable: true, value: scrollTo },
    });
    scroller.getBoundingClientRect = () => ({ top: 0, bottom: 600, left: 0, right: 800, width: 800, height: 600, x: 0, y: 0, toJSON: () => ({}) });
    anchors[0].getBoundingClientRect = () => ({ top: 40, bottom: 80, left: 0, right: 0, width: 0, height: 40, x: 0, y: 40, toJSON: () => ({}) });
    anchors[1].getBoundingClientRect = () => ({ top: 640, bottom: 680, left: 0, right: 0, width: 0, height: 40, x: 0, y: 640, toJSON: () => ({}) });

    expect(screen.getAllByRole("button", { name: /跳转到/ })).toHaveLength(2);
    expect(screen.getByRole("button", { name: "02第二个问题" })).toHaveAttribute("aria-current", "true");

    fireEvent.click(screen.getByRole("button", { name: "01第一个问题" }));
    expect(scrollTo).toHaveBeenCalledWith({ top: 16, behavior: "smooth" });
    expect(screen.getByRole("button", { name: "01第一个问题" })).toHaveAttribute("aria-current", "true");
  });
});
