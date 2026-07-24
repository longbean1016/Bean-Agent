import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";

import type { ChatMessage } from "./types";

const DEFAULT_PREVIEW_LENGTH = 34;

export interface TurnNavigationEntry {
  id: string;
  question: string;
  preview: string;
}

interface VerticalRegion {
  top: number;
  bottom: number;
}

export interface NavigationMessage {
  message: ChatMessage;
  navigationTurnId: string;
}

export interface NavigationTurnGroup {
  navigationTurnId: string;
  messages: ChatMessage[];
}

export function activeTurnFromVisibleRegions(
  turns: TurnNavigationEntry[],
  regions: ReadonlyMap<string, VerticalRegion[]>,
  viewportTop: number,
  viewportBottom: number,
  atConversationEnd = false,
  direction: "up" | "down" = "down",
): string {
  if (atConversationEnd) return turns.at(-1)?.id ?? "";
  const orderedTurns = direction === "up" ? [...turns].reverse() : turns;
  for (const turn of orderedTurns) {
    if (regions.get(turn.id)?.some((region) => (
      region.bottom > viewportTop && region.top < viewportBottom
    ))) return turn.id;
  }
  return turns[0]?.id ?? "";
}

export function scrollTopToRevealItem(
  scrollTop: number,
  containerTop: number,
  containerBottom: number,
  itemTop: number,
  itemBottom: number,
): number {
  if (itemTop < containerTop) return scrollTop - (containerTop - itemTop);
  if (itemBottom > containerBottom) return scrollTop + (itemBottom - containerBottom);
  return scrollTop;
}

export function messagesWithNavigationTurns(messages: ChatMessage[]): NavigationMessage[] {
  let navigationTurnId = "";
  return messages.map((message) => {
    if (message.role === "user") navigationTurnId = message.turnId || message.id;
    if (message.proactive) navigationTurnId = "";
    return { message, navigationTurnId };
  });
}

export function groupMessagesIntoNavigationTurns(messages: ChatMessage[]): NavigationTurnGroup[] {
  const groups: NavigationTurnGroup[] = [];
  for (const item of messagesWithNavigationTurns(messages)) {
    const current = groups.at(-1);
    if (current && current.navigationTurnId === item.navigationTurnId) {
      current.messages.push(item.message);
    } else {
      groups.push({ navigationTurnId: item.navigationTurnId, messages: [item.message] });
    }
  }
  return groups;
}

export function turnsFromMessages(
  messages: ChatMessage[],
  previewLength = DEFAULT_PREVIEW_LENGTH,
): TurnNavigationEntry[] {
  return messages
    .filter((message) => message.role === "user")
    .map((message) => {
      const question = message.content.replace(/\s+/g, " ").trim() || "附件消息";
      const preview = question.length > previewLength
        ? `${question.slice(0, previewLength).trimEnd()}…`
        : question;
      return {
        id: message.turnId || message.id,
        question,
        preview,
      };
    });
}

export function TurnNavigator({ sessionId, turns }: {
  sessionId: string;
  turns: TurnNavigationEntry[];
}) {
  const navRef = useRef<HTMLElement>(null);
  const previousScrollTopRef = useRef(0);
  const scrollDirectionRef = useRef<"up" | "down">("down");
  const navigationLockRef = useRef("");
  const navigationUnlockTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [activeTurnId, setActiveTurnId] = useState(() => turns.at(-1)?.id ?? "");

  const scheduleNavigationUnlock = useCallback((delay: number) => {
    if (navigationUnlockTimerRef.current) clearTimeout(navigationUnlockTimerRef.current);
    navigationUnlockTimerRef.current = setTimeout(() => {
      navigationLockRef.current = "";
      navigationUnlockTimerRef.current = null;
    }, delay);
  }, []);

  const findScroller = useCallback(() => (
    navRef.current?.closest(".conversation")?.querySelector<HTMLElement>(".conversation-scroll") ?? null
  ), []);

  const findAnchor = useCallback((id: string) => {
    const scroller = findScroller();
    if (!scroller) return null;
    return Array.from(scroller.querySelectorAll<HTMLElement>("[data-turn-anchor]"))
      .find((element) => element.dataset.turnAnchor === id) ?? null;
  }, [findScroller]);

  const jumpToTurn = useCallback((id: string) => {
    const scroller = findScroller();
    const anchor = findAnchor(id);
    if (!scroller || !anchor) return;
    const top = scroller.scrollTop
      + anchor.getBoundingClientRect().top
      - scroller.getBoundingClientRect().top
      - 24;
    navigationLockRef.current = id;
    scheduleNavigationUnlock(1200);
    scroller.scrollTo({ top: Math.max(0, top), behavior: "smooth" });
    setActiveTurnId(id);
  }, [findAnchor, findScroller, scheduleNavigationUnlock]);

  useEffect(() => () => {
    if (navigationUnlockTimerRef.current) clearTimeout(navigationUnlockTimerRef.current);
  }, []);

  useEffect(() => {
    setActiveTurnId(turns.at(-1)?.id ?? "");
  }, [sessionId, turns.length]);

  useEffect(() => {
    const scroller = findScroller();
    if (!scroller || turns.length === 0) return;
    previousScrollTopRef.current = scroller.scrollTop;
    const updateActiveTurn = () => {
      if (navigationLockRef.current) {
        previousScrollTopRef.current = scroller.scrollTop;
        setActiveTurnId(navigationLockRef.current);
        scheduleNavigationUnlock(180);
        return;
      }
      if (scroller.scrollTop < previousScrollTopRef.current) scrollDirectionRef.current = "up";
      if (scroller.scrollTop > previousScrollTopRef.current) scrollDirectionRef.current = "down";
      previousScrollTopRef.current = scroller.scrollTop;
      const scrollerRect = scroller.getBoundingClientRect();
      const regions = new Map<string, VerticalRegion[]>();
      for (const element of scroller.querySelectorAll<HTMLElement>("[data-turn-region]")) {
        const id = element.dataset.turnRegion;
        if (!id) continue;
        const rect = element.getBoundingClientRect();
        regions.set(id, [...(regions.get(id) ?? []), { top: rect.top, bottom: rect.bottom }]);
      }
      const atConversationEnd = scroller.scrollHeight - scroller.clientHeight - scroller.scrollTop <= 1;
      setActiveTurnId(activeTurnFromVisibleRegions(
        turns,
        regions,
        scrollerRect.top,
        scrollerRect.bottom,
        atConversationEnd,
        scrollDirectionRef.current,
      ));
    };
    updateActiveTurn();
    scroller.addEventListener("scroll", updateActiveTurn, { passive: true });
    window.addEventListener("resize", updateActiveTurn);
    return () => {
      scroller.removeEventListener("scroll", updateActiveTurn);
      window.removeEventListener("resize", updateActiveTurn);
    };
  }, [findAnchor, findScroller, scheduleNavigationUnlock, sessionId, turns]);

  useLayoutEffect(() => {
    const reveal = (selector: string) => {
      const item = navRef.current?.querySelector<HTMLElement>(selector);
      const container = item?.parentElement;
      if (!item || !container) return;
      const containerRect = container.getBoundingClientRect();
      const itemRect = item.getBoundingClientRect();
      container.scrollTop = scrollTopToRevealItem(
        container.scrollTop,
        containerRect.top,
        containerRect.bottom,
        itemRect.top,
        itemRect.bottom,
      );
    };
    if (activeTurnId) {
      reveal(`[data-turn-marker="${activeTurnId}"]`);
      reveal(`[data-turn-nav-item="${activeTurnId}"]`);
    }
  }, [activeTurnId]);

  if (turns.length === 0) return null;

  return (
    <nav ref={navRef} className="turn-navigator" aria-label="会话轮次导航">
      <div className="turn-marker-rail" aria-label="会话轮次刻度">
        {turns.map((turn) => (
          <button
            key={turn.id}
            type="button"
            className={`turn-marker ${activeTurnId === turn.id ? "active" : ""}`}
            aria-label={`跳转到：${turn.preview}`}
            aria-current={activeTurnId === turn.id ? "true" : undefined}
            data-turn-marker={turn.id}
            onClick={() => jumpToTurn(turn.id)}
          />
        ))}
      </div>
      <div className="turn-directory" aria-label="本会话问题目录">
        <div className="turn-directory-list">
          {turns.map((turn, index) => (
            <button
              key={turn.id}
              type="button"
              className={`turn-directory-item ${activeTurnId === turn.id ? "active" : ""}`}
              aria-current={activeTurnId === turn.id ? "true" : undefined}
              data-turn-nav-item={turn.id}
              title={turn.question}
              onClick={() => jumpToTurn(turn.id)}
            >
              <span className="turn-directory-index">{String(index + 1).padStart(2, "0")}</span>
              <span>{turn.preview}</span>
            </button>
          ))}
        </div>
      </div>
    </nav>
  );
}
