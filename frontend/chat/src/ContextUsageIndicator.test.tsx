import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ContextUsageIndicator } from "./App";
import type { ContextUsage } from "./types";
import type { ModelProfile } from "./types";

const usage: ContextUsage = {
  turnId: "turn-1",
  usedTokens: 65500,
  pressureTokens: 65500,
  contextWindow: 1_000_000,
  softLimitTokens: 740_000,
  hardInputTokens: 991_808,
  contextWindowSource: "provider_catalog",
  estimateSource: "heuristic",
  breakdown: {
    system_prompt_tokens: 1600,
    tools_tokens: 6900,
    conversation_tokens: 49700,
    overhead_tokens: 7300,
  },
  sections: [],
};

describe("ContextUsageIndicator", () => {
  it("显示环、悬停百分比并点击展开详情", () => {
    render(<ContextUsageIndicator usage={usage} compacting={false} />);

    const button = screen.getByRole("button", { name: "上下文已用 7%" });
    expect(button.querySelector(".context-usage-ring")).not.toBeNull();
    expect(screen.getByRole("tooltip")).toHaveTextContent("上下文已用 7%");

    fireEvent.click(button);
    expect(screen.getByRole("dialog", { name: "上下文占用详情" })).toHaveTextContent("~65.5K / 1M");
    expect(screen.getByRole("dialog")).toHaveTextContent("系统提示词");

    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("未知窗口不显示圆圈", () => {
    const result = render(<ContextUsageIndicator compacting usage={{ ...usage, contextWindow: 0 }} />);

    expect(result.container).toBeEmptyDOMElement();
  });

  it("尚无 usage 时显示所选模型容量，未知容量显示空状态", () => {
    const profile = {
      connection_id: "connection-1", model_id: "model-1", display_name: "Model 1",
      context_window: 128000, max_output_tokens: 8192, supports_tools: true,
      supports_vision: false, supports_reasoning: false, reasoning_options: [],
      adapter: "generic_openai", metadata_source: "models.dev:test",
      metadata_updated_at: null, user_overrides: {}, available: true, revision: 1,
      discovered_at: "2026-09-03T00:00:00Z",
    } satisfies ModelProfile;
    const { rerender } = render(<ContextUsageIndicator compacting={false} profile={profile} />);
    expect(screen.getByRole("button", { name: "上下文容量 128K" })).toHaveTextContent("128K");

    rerender(<ContextUsageIndicator compacting={false} profile={{ ...profile, context_window: null }} />);
    expect(screen.getByRole("button", { name: "上下文容量未知" })).toHaveTextContent("--");
  });
});
