export const REASONING_CHOICES = [
  "none", "enabled", "minimal", "low", "medium", "high", "xhigh", "max",
] as const;

export function reasoningOptionsForModel(model: {
  reasoning_options: string[];
  capabilities_json?: { reasoning?: { native?: string[] } };
}): string[] {
  const native = model.capabilities_json?.reasoning?.native;
  return native?.length ? native : model.reasoning_options;
}

export function reasoningStatusForModel(model: {
  supports_reasoning: boolean | null;
  capability_confidence?: string;
}): string {
  if (model.supports_reasoning === false) return "思考关闭";
  if (model.supports_reasoning == null) return "思考能力待验证";
  if (model.capability_confidence === "high") return "思考已验证";
  return "思考能力待验证";
}

export function updateReasoningOptions(
  current: string[],
  option: string,
  checked: boolean,
): string[] {
  let next = checked
    ? [...new Set([...current, option])]
    : current.filter((item) => item !== option);
  if (checked && option === "enabled") {
    next = next.filter((item) => !isExplicitEffort(item));
  } else if (checked && isExplicitEffort(option)) {
    next = next.filter((item) => item !== "enabled");
  }
  return REASONING_CHOICES.filter((item) => next.includes(item));
}

function isExplicitEffort(value: string): boolean {
  return !["none", "enabled"].includes(value);
}
