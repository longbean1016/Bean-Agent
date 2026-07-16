import { afterEach, describe, expect, it, vi } from "vitest";

import { BeanWebSocketClient } from "./websocketClient";

class FakeSocket {
  static instances: FakeSocket[] = [];
  readyState: number = WebSocket.CONNECTING;
  sent: string[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: (() => void) | null = null;

  constructor() { FakeSocket.instances.push(this); }
  open() { this.readyState = WebSocket.OPEN; this.onopen?.(); }
  send(value: string) { this.sent.push(value); }
  close() { this.readyState = WebSocket.CLOSED; this.onclose?.(); }
  receive(value: string) { this.onmessage?.({ data: value } as MessageEvent); }
}

afterEach(() => {
  FakeSocket.instances = [];
  vi.useRealTimers();
});

describe("BeanWebSocketClient", () => {
  it("断线后按退避时间自动重连", () => {
    vi.useFakeTimers();
    const statuses: string[] = [];
    const client = new BeanWebSocketClient({
      onFrame: vi.fn(),
      onStatus: (status) => statuses.push(status),
      socketFactory: () => new FakeSocket() as unknown as WebSocket,
    });

    client.connect();
    FakeSocket.instances[0].open();
    FakeSocket.instances[0].close();
    expect(statuses).toEqual(["connecting", "connected", "reconnecting"]);

    vi.advanceTimersByTime(750);
    expect(FakeSocket.instances).toHaveLength(2);
    client.close();
  });

  it("把无效服务端 JSON 转换成结构化错误帧", () => {
    const frames: unknown[] = [];
    const client = new BeanWebSocketClient({
      onFrame: (frame) => frames.push(frame),
      onStatus: vi.fn(),
      socketFactory: () => new FakeSocket() as unknown as WebSocket,
    });

    client.connect();
    FakeSocket.instances[0].open();
    FakeSocket.instances[0].receive("not-json");

    expect(frames).toEqual([expect.objectContaining({ type: "error", code: "invalid_server_frame" })]);
    client.close();
  });
});
