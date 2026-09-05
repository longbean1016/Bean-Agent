import { expect, it } from "vitest";

import { REASONING_CHOICES, updateReasoningOptions } from "./reasoning";

it("区分推理开关和明确强度", () => {
  expect(REASONING_CHOICES).toContain("enabled");
  expect(updateReasoningOptions(["none", "enabled"], "high", true)).toEqual(["none", "high"]);
  expect(updateReasoningOptions(["none", "high"], "enabled", true)).toEqual(["none", "enabled"]);
});
