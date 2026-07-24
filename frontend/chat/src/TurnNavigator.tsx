import { useCallback, useEffect, useRef, useState } from "react";

import type { ChatMessage } from "./types";

const DEFAULT_PREVIEW_LENGTH = 34;

export interface TurnNavigationEntry {
  id: string;
  question: string;
  preview: string;
}

export function activeTurnAtViewportTop(
  turns: TurnNavigationEntry[],
  anchorTops: ReadonlyMap<string, number>,
  viewportTop: number,
  atConversationEnd = false,
): string {
  if (atConversationEnd) return turns.at(-1)?.id ?? "";
  let current = turns[0]?.id ?? "";
  for (const turn of turns) {
    const anchorTop = anchorTops.get(turn.id);
    if (anchorTop !== undefined && anchorTop <= viewportTop) current = turn.id;
  }
  return current;
}

export function scrollTopToRevealItem(
  scrollTop: number,
  clientHeight: number,
  itemTop: number,
  itemHeight: number,
): number {
  if (itemTop < scrollTop) return itemTop;
  const itemBottom = itemTop + itemHeight;
  if (itemBottom > scrollTop + clientHeight) return itemBottom - clientHeight;
  return scrollTop;
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
  const [activeTurnId, setActiveTurnId] = useState(() => turns.at(-1)?.id ?? "");

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
    scroller.scrollTo({ top: Math.max(0, top), behavior: "smooth" });
    setActiveTurnId(id);
  }, [findAnchor, findScroller]);

  useEffect(() => {
    setActiveTurnId(turns.at(-1)?.id ?? "");
  }, [sessionId, turns.length]);

  useEffect(() => {
    const scroller = findScroller();
    if (!scroller || turns.length === 0) return;
    const updateActiveTurn = () => {
      const scrollerRect = scroller.getBoundingClientRect();
      const anchorTops = new Map(turns.flatMap((turn) => {
        const anchor = findAnchor(turn.id);
        return anchor ? [[turn.id, anchor.getBoundingClientRect().top] as const] : [];
      }));
      const atConversationEnd = scroller.scrollHeight - scroller.clientHeight - scroller.scrollTop <= 1;
      setActiveTurnId(activeTurnAtViewportTop(
        turns,
        anchorTops,
        scrollerRect.top,
        atConversationEnd,
      ));
    };
    updateActiveTurn();
    scroller.addEventListener("scroll", updateActiveTurn, { passive: true });
    window.addEventListener("resize", updateActiveTurn);
    return () => {
      scroller.removeEventListener("scroll", updateActiveTurn);
      window.removeEventListener("resize", updateActiveTurn);
    };
  }, [findAnchor, findScroller, sessionId, turns]);

  useEffect(() => {
    const reveal = (selector: string) => {
      const item = navRef.current?.querySelector<HTMLElement>(selector);
      const container = item?.parentElement;
      if (!item || !container) return;
      container.scrollTop = scrollTopToRevealItem(
        container.scrollTop,
        container.clientHeight,
        item.offsetTop,
        item.offsetHeight,
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
