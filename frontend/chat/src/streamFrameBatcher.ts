import type { ChatFrame } from "./types";

type TextFrame = Extract<ChatFrame, { type: "answer.delta" | "react.thinking.delta" }>;

/** 只合并相邻的同源文本；控制帧是顺序屏障，不延迟审批或终态。 */
export class StreamFrameBatcher {
  private pending: TextFrame | null = null;
  private chunks: string[] = [];
  private frameId: number | null = null;
  private timerId: number | null = null;

  constructor(private readonly emit: (frame: ChatFrame) => void) {}

  push(frame: ChatFrame): void {
    if (frame.type !== "answer.delta" && frame.type !== "react.thinking.delta") {
      this.flush();
      this.emit(frame);
      return;
    }
    if (this.pending && (this.pending.type !== frame.type
      || this.pending.session_id !== frame.session_id || this.pending.turn_id !== frame.turn_id
      || this.pending.part_id !== frame.part_id)) {
      this.flush();
    }
    this.pending = frame;
    this.chunks.push(frame.delta);
    if (this.frameId !== null) return;
    this.frameId = window.requestAnimationFrame(() => this.flush());
    // 后台标签页可能暂停绘制；终态仍会同步 flush，此定时器只限制普通文本积压。
    this.timerId = window.setTimeout(() => this.flush(), 50);
  }

  flush(): void {
    const pending = this.pending;
    const delta = this.chunks.join("");
    this.cancel();
    if (pending) this.emit({ ...pending, delta });
  }

  cancel(): void {
    if (this.frameId !== null) window.cancelAnimationFrame(this.frameId);
    if (this.timerId !== null) window.clearTimeout(this.timerId);
    this.frameId = null;
    this.timerId = null;
    this.pending = null;
    this.chunks = [];
  }
}
