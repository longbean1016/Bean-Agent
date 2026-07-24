import { useCallback, useEffect, useRef, useState } from "react";

import type { ChatMessage } from "./types";

const DEFAULT_PREVIEW_LENGTH = 34;

export interface TurnNavigationEntry {
  id: string;
  question: string;
  preview: string;
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
      const probe = scrollerRect.top + Math.min(scroller.clientHeight * 0.28, 180);
      let current = turns[0].id;
      for (const turn of turns) {
        const anchor = findAnchor(turn.id);
        if (anchor && anchor.getBoundingClientRect().top <= probe) current = turn.id;
      }
      setActiveTurnId(current);
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
    const activeItem = navRef.current?.querySelector<HTMLElement>(
      `[data-turn-nav-item="${activeTurnId}"]`,
    );
    if (activeItem && typeof activeItem.scrollIntoView === "function") {
      activeItem.scrollIntoView({ block: "nearest" });
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
