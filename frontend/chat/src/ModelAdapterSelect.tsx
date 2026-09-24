import { Check, ChevronDown } from "lucide-react";
import { useEffect, useId, useRef, useState } from "react";

import type { ModelAdapterId } from "./types";

export const MODEL_ADAPTER_OPTIONS: ReadonlyArray<{ id: ModelAdapterId; label: string }> = [
  { id: "generic_openai", label: "通用 OpenAI" },
  { id: "deepseek", label: "DeepSeek" },
  { id: "qwen_dashscope", label: "Qwen / DashScope" },
  { id: "openai_reasoning", label: "OpenAI Reasoning" },
];

type ModelAdapterSelectProps = {
  value: ModelAdapterId;
  onChange: (value: ModelAdapterId) => void;
  ariaLabel: string;
  disabled?: boolean;
};

export function ModelAdapterSelect({ value, onChange, ariaLabel, disabled = false }: ModelAdapterSelectProps) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const optionRefs = useRef<Array<HTMLButtonElement | null>>([]);
  const menuId = useId();
  const selectedIndex = Math.max(0, MODEL_ADAPTER_OPTIONS.findIndex((option) => option.id === value));
  const selectedOption = MODEL_ADAPTER_OPTIONS[selectedIndex];

  useEffect(() => {
    if (!open) return;
    optionRefs.current[selectedIndex]?.focus();

    const closeWhenPointerLeaves = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("pointerdown", closeWhenPointerLeaves);
    return () => document.removeEventListener("pointerdown", closeWhenPointerLeaves);
  }, [open, selectedIndex]);

  const focusOption = (index: number) => {
    const nextIndex = (index + MODEL_ADAPTER_OPTIONS.length) % MODEL_ADAPTER_OPTIONS.length;
    optionRefs.current[nextIndex]?.focus();
  };

  return (
    <div className="model-adapter-select" ref={rootRef}>
      <button
        type="button"
        className="model-adapter-trigger"
        aria-label={`${ariaLabel}：${selectedOption.label}`}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={menuId}
        disabled={disabled}
        onClick={() => setOpen((current) => !current)}
        onKeyDown={(event) => {
          if (event.key === "ArrowDown" || event.key === "ArrowUp") {
            event.preventDefault();
            setOpen(true);
          }
        }}
      >
        <span>{selectedOption.label}</span>
        <ChevronDown size={15} aria-hidden="true" />
      </button>
      {open ? <div id={menuId} className="model-adapter-menu" role="listbox" aria-label={`${ariaLabel}选项`}>
        {MODEL_ADAPTER_OPTIONS.map((option, index) => (
          <button
            key={option.id}
            ref={(element) => { optionRefs.current[index] = element; }}
            type="button"
            role="option"
            aria-selected={option.id === value}
            onClick={() => {
              onChange(option.id);
              setOpen(false);
            }}
            onKeyDown={(event) => {
              if (event.key === "ArrowDown") {
                event.preventDefault();
                focusOption(index + 1);
              } else if (event.key === "ArrowUp") {
                event.preventDefault();
                focusOption(index - 1);
              } else if (event.key === "Home") {
                event.preventDefault();
                focusOption(0);
              } else if (event.key === "End") {
                event.preventDefault();
                focusOption(MODEL_ADAPTER_OPTIONS.length - 1);
              } else if (event.key === "Escape") {
                event.preventDefault();
                setOpen(false);
                rootRef.current?.querySelector<HTMLButtonElement>(".model-adapter-trigger")?.focus();
              }
            }}
          >
            <span>{option.label}</span>
            <Check size={15} aria-hidden="true" />
          </button>
        ))}
      </div> : null}
    </div>
  );
}
