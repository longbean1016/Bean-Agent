import { cleanup, render } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { MessageView } from "./App";
import type { ChatMessage } from "./types";

const { markdownRender } = vi.hoisted(() => ({ markdownRender: vi.fn() }));
vi.mock("streamdown", () => ({ Streamdown: ({ children }: { children: string }) => { markdownRender(children); return <div>{children}</div>; } }));
afterEach(() => { cleanup(); markdownRender.mockClear(); });

it("活动消息更新 20 次时，100 条未变化历史正文不重复解析", () => {
  const history: ChatMessage[] = Array.from({ length: 100 }, (_, index) => ({ id: `message-${index}`, role: "assistant", content: `历史 ${index}`, thinking: "", media: [], tools: [] }));
  const active: ChatMessage = { id: "active", turnId: "turn", role: "assistant", content: "起始", thinking: "", media: [], tools: [], streaming: true };
  const tree = (message: ChatMessage) => <>{[...history, message].map((item) => <MessageView key={item.id} message={item}
    navigationTurnId="" approvals={[]} approvalDecisionRequests={{}} />)}</>;
  const view = render(tree(active));
  expect(markdownRender).toHaveBeenCalledTimes(101);
  markdownRender.mockClear();
  for (let index = 0; index < 20; index += 1) view.rerender(tree({ ...active, content: `增量 ${index}` }));
  expect(markdownRender).toHaveBeenCalledTimes(20);
  expect(markdownRender.mock.calls.every(([text]) => text.startsWith("增量"))).toBe(true);
});
