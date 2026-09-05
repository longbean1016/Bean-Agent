export const REASONING_CHOICES = [
  "none", "enabled", "minimal", "low", "medium", "high", "xhigh", "max",
] as const;

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
