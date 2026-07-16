import type { ChatFrame, ConnectionStatus } from "./types";

type WebSocketFactory = (url: string) => WebSocket;

interface ClientOptions {
  onFrame: (frame: ChatFrame) => void;
  onStatus: (status: ConnectionStatus) => void;
  socketFactory?: WebSocketFactory;
}

export class BeanWebSocketClient {
  private socket: WebSocket | null = null;
  private reconnectTimer: number | null = null;
  private reconnectAttempt = 0;
  private closed = false;
  private readonly socketFactory: WebSocketFactory;

  constructor(private readonly options: ClientOptions) {
    this.socketFactory = options.socketFactory ?? ((url) => new WebSocket(url));
  }

  connect(): void {
    if (this.socket && this.socket.readyState <= WebSocket.OPEN) return;
    this.closed = false;
    this.options.onStatus(this.reconnectAttempt ? "reconnecting" : "connecting");
    const protocol = window.location.protocol === "https:" ? "wss" : "ws";
    const socket = this.socketFactory(`${protocol}://${window.location.host}/ws`);
    this.socket = socket;
    socket.onopen = () => {
      this.reconnectAttempt = 0;
      this.options.onStatus("connected");
    };
    socket.onmessage = (event) => {
      try {
        this.options.onFrame(JSON.parse(String(event.data)) as ChatFrame);
      } catch {
        this.options.onFrame({ type: "error", request_id: "", code: "invalid_server_frame", message: "服务端返回了无法解析的消息" });
      }
    };
    socket.onerror = () => socket.close();
    socket.onclose = () => {
      if (this.socket === socket) this.socket = null;
      if (this.closed) {
        this.options.onStatus("offline");
        return;
      }
      this.scheduleReconnect();
    };
  }

  send(payload: Record<string, unknown>): boolean {
    if (!this.socket || this.socket.readyState !== WebSocket.OPEN) return false;
    this.socket.send(JSON.stringify(payload));
    return true;
  }

  reconnectNow(): void {
    if (this.reconnectTimer !== null) window.clearTimeout(this.reconnectTimer);
    this.reconnectTimer = null;
    this.socket?.close();
    this.socket = null;
    this.reconnectAttempt = 0;
    this.connect();
  }

  close(): void {
    this.closed = true;
    if (this.reconnectTimer !== null) window.clearTimeout(this.reconnectTimer);
    this.reconnectTimer = null;
    this.socket?.close();
    this.socket = null;
  }

  private scheduleReconnect(): void {
    this.reconnectAttempt += 1;
    this.options.onStatus("reconnecting");
    // 重连退避封顶 12 秒，避免断网时形成高频连接风暴。
    const delay = Math.min(12_000, 750 * 2 ** (this.reconnectAttempt - 1));
    this.reconnectTimer = window.setTimeout(() => {
      this.reconnectTimer = null;
      this.connect();
    }, delay);
  }
}
