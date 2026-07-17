# Chat Scroll Theme Mermaid Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复流式滚动控制，增加三态主题，并确保 Mermaid 永远按普通代码显示。

**Architecture:** 三项行为都收敛在聊天前端：滚动由会话容器事件和跟随状态控制；主题由持久化偏好与系统媒体查询驱动 CSS 变量；Mermaid 在 Markdown 输入边界文本化。每项独立测试和提交。

**Tech Stack:** React 19、TypeScript、Vitest、Streamdown、CSS variables、Lucide。

---

### Task 1: 用户可中断的流式跟随

**Files:**
- Modify: `frontend/chat/src/App.tsx`
- Modify: `frontend/chat/src/styles.css`
- Test: `frontend/chat/src/App.test.tsx`

- [ ] 新增测试：用户向上滚动后收到 delta 不再滚到底部，点击回到底部按钮后恢复。
- [ ] 运行聚焦测试并确认 RED。
- [ ] 实现底部距离检测、跟随 ref/state、恢复按钮和发送/切换会话恢复逻辑。
- [ ] 运行聚焦测试和类型检查，提交 `fix: 允许流式输出时向上滚动`。

### Task 2: 三态主题

**Files:**
- Modify: `frontend/chat/src/App.tsx`
- Modify: `frontend/chat/src/styles.css`
- Test: `frontend/chat/src/App.test.tsx`

- [ ] 新增测试：默认跟随系统、切换深色/浅色、持久化选择及系统变化响应。
- [ ] 运行聚焦测试并确认 RED。
- [ ] 实现主题状态、matchMedia 监听、右上角三段控件和深浅 CSS 变量。
- [ ] 运行聚焦测试和类型检查，提交 `feature: 增加三态主题切换`。

### Task 3: Mermaid 文本化防线

**Files:**
- Modify: `frontend/chat/src/App.tsx`
- Test: `frontend/chat/src/App.test.tsx`

- [ ] 扩展测试，断言 Mermaid fence 的展示语言为 text 且正文原样保留。
- [ ] 运行测试并确认 RED。
- [ ] 在 Markdown 预处理器中仅改写 fence 起始行的 `mermaid` info string。
- [ ] 运行完整前端单测、类型检查、生产构建和 `git diff --check`，提交
  `fix: 强制 Mermaid 以文本代码显示`。
