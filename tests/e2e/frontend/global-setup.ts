import { execFileSync, spawn, type ChildProcess } from "node:child_process";

let server: ChildProcess | null = null;

export default async function globalSetup(): Promise<() => Promise<void>> {
  const python = process.env.PYTHON || "python";
  server = spawn(python, ["tests/e2e/frontend/fake_server.py"], {
    cwd: process.cwd(),
    stdio: "ignore",
    windowsHide: true,
  });
  await waitUntilReady(server);

  return async () => {
    if (!server?.pid) return;
    // Windows 的 Uvicorn 子进程不会稳定响应 Playwright 的普通 SIGTERM；按本次
    // 启动记录的 PID 关闭进程树，避免误伤用户已有的 Python 服务。
    if (process.platform === "win32") {
      try {
        execFileSync("taskkill", ["/pid", String(server.pid), "/T", "/F"], { stdio: "ignore" });
      } catch {
        // 进程已经自行退出时 taskkill 会返回非零，这不属于测试失败。
      }
    } else {
      server.kill("SIGTERM");
    }
    server = null;
  };
}

async function waitUntilReady(child: ChildProcess): Promise<void> {
  const deadline = Date.now() + 15_000;
  while (Date.now() < deadline) {
    if (child.exitCode !== null) throw new Error(`Fake 服务提前退出: ${child.exitCode}`);
    try {
      const response = await fetch("http://127.0.0.1:4173/");
      if (response.ok) return;
    } catch {
      // 服务启动窗口内连接失败是正常状态，下一轮继续探测。
    }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error("Fake 服务在 15 秒内未就绪");
}
