import * as Collapsible from "@radix-ui/react-collapsible";
import { Atom, Check, ChevronDown, LoaderCircle, Search } from "lucide-react";
import { memo, useEffect, useState, type ReactNode } from "react";
import type { ApprovalDecision, ApprovalRequest, ChatMessage, ToolActivity } from "./types";
import type { ProcessPart } from "./turnPresentation";
import { ToolTimeline, formatCompactDuration, type ToolDisclosureState } from "./ToolTimeline";
import { MemoryResults } from "./MemoryResults";
import "./turnProcess.css";

type Props = {
  message: ChatMessage; parts: ProcessPart[]; disclosure: ToolDisclosureState; disclosureKey: string;
  approvals: ApprovalRequest[]; approvalDecisionRequests: Record<string, string>;
  onApprovalDecision?: (approval: ApprovalRequest, decision: ApprovalDecision) => void;
  renderText: (text: string, streaming: boolean) => ReactNode;
};

export const TurnProcess = memo(function TurnProcess({ message, parts, disclosure, disclosureKey,
  approvals, approvalDecisionRequests, onApprovalDecision, renderText }: Props) {
  const final = Boolean(message.presentation?.final || !message.streaming);
  const phaseKey = `${disclosureKey}:process:${final ? "final" : "running"}`;
  const [choice, setChoice] = useState<{ key: string; open: boolean } | null>(null);
  // 每个阶段只应用一次默认值；用户在最终正文生成期间手动展开后不再被流式更新覆盖。
  const open = choice?.key === phaseKey ? choice.open : disclosure.get(phaseKey) ?? !final;
  const toolProps = { disclosure, disclosureKey, approvalDecisionRequests, onApprovalDecision };
  const toolsById = new Map(message.tools.map((tool) => [tool.callId, tool]));
  return <section className="turn-process">
    <Collapsible.Root open={open} onOpenChange={(value) => {
      disclosure.set(phaseKey, value); setChoice({ key: phaseKey, open: value });
    }}>
      <Collapsible.Trigger className="turn-work-trigger">
        {message.streaming ? <LoaderCircle size={14} className="turn-work-spinner" aria-hidden="true" /> : null}
        <WorkDuration message={message} />
        <ChevronDown size={14} aria-hidden="true" />
      </Collapsible.Trigger>
      <Collapsible.Content className="turn-process-content">
        {parts.map((part) => <ProcessItem key={part.id} part={part}
          tool={part.kind === "tool" ? toolsById.get(part.call_id) : undefined}
          disclosure={disclosure} disclosureKey={disclosureKey} renderText={renderText}
          thinkingRunning={Boolean(message.streaming && !final && (
            message.presentation ? message.presentation.parts.at(-1)?.id === part.id
              : parts.at(-1)?.id === part.id && !message.content
          ))}
          interrupted={message.status === "interrupted"} />)}
      </Collapsible.Content>
    </Collapsible.Root>
    {/* 授权动作不属于可隐藏的阅读详情，沿用原卡片与回调。 */}
    {approvals.length ? <ToolTimeline {...toolProps} tools={[]} approvals={approvals} /> : null}
  </section>;
});

// 已完成段落保持引用稳定，正文分片不能触发全部历史过程的 Markdown 重渲染。
const ProcessItem = memo(function ProcessItem({ part, tool, disclosure, disclosureKey, renderText, thinkingRunning, interrupted }: {
  part: ProcessPart; tool?: ToolActivity; disclosure: ToolDisclosureState; disclosureKey: string;
  renderText: Props["renderText"]; thinkingRunning: boolean; interrupted: boolean;
}) {
  if (part.kind === "tool") return tool ? <ToolTimeline disclosure={disclosure} disclosureKey={disclosureKey}
    tools={[part.memory_result ? { ...tool, memoryResult: part.memory_result } : tool]}
    approvals={[]} approvalDecisionRequests={{}} /> : null;
  if (part.kind === "memory") return <ProcessDisclosure id={`${disclosureKey}:${part.id}`} disclosure={disclosure}
    label={`已检索记忆 · 找到 ${part.items.length} 条`} icon={<Search size={16} />}>
    <MemoryResults query={part.query} items={part.items} origin="本轮自动检索" />
    {part.duration_ms !== undefined ? <p className="process-detail-time">检索耗时 {formatCompactDuration(part.duration_ms)}</p> : null}
  </ProcessDisclosure>;
  if (part.kind === "thinking") return <ProcessDisclosure id={`${disclosureKey}:${part.id}`} disclosure={disclosure}
    label={thinkingRunning ? "正在思考…" : interrupted ? "思考已停止" : "思考"} icon={<Atom size={16} />}>
    {renderText(part.text, false)}
  </ProcessDisclosure>;
  return part.text ? <div className="process-intermediate">{renderText(part.text, false)}</div> : null;
});

function WorkDuration({ message }: { message: ChatMessage }) {
  const [now, setNow] = useState(Date.now);
  const start = Date.parse(message.presentation?.started_at ?? "");
  useEffect(() => {
    if (!message.streaming || !Number.isFinite(start)) return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [message.streaming, start]);
  const elapsed = message.streaming ? Number.isFinite(start) ? Math.max(0, now - start) : undefined : message.durationMs;
  const label = message.streaming ? "工作中" : message.status === "interrupted" ? "已停止" : message.status === "error" ? "工作未完成" : "已工作";
  return <span>{label}{elapsed === undefined ? "" : ` ${formatCompactDuration(elapsed)}`}</span>;
}

function ProcessDisclosure({ id, disclosure, label, icon, children }: {
  id: string; disclosure: ToolDisclosureState; label: string; icon: ReactNode; children: ReactNode;
}) {
  const [open, setOpen] = useState(() => disclosure.get(id) ?? false);
  return <Collapsible.Root className="process-disclosure" open={open} onOpenChange={(value) => {
    disclosure.set(id, value); setOpen(value);
  }}>
    <Collapsible.Trigger className="process-row">{icon}<span>{label}</span>
      {label.startsWith("已检索") ? <Check size={13} className="process-success" /> : null}
      <ChevronDown size={14} />
    </Collapsible.Trigger>
    <Collapsible.Content className="process-details">{children}</Collapsible.Content>
  </Collapsible.Root>;
}
