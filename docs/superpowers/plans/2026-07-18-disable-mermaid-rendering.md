# Disable Mermaid Rendering Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 Mermaid fenced block 回退为稳定的普通代码块，彻底移除聊天中的交互图表。

**Architecture:** Streamdown 的插件注册是唯一能力开关；移除 Mermaid 插件后沿用现有 code 插件渲染源码。空状态示例和 npm 直接依赖同步删除，避免 UI 提示与 bundle 能力漂移。

**Tech Stack:** React 19、Streamdown、Vitest、Vite、npm。

---

### Task 1: 锁定 Mermaid 回退行为

**Files:**
- Modify: `frontend/chat/src/App.test.tsx`

- [ ] **Step 1: Write the failing test**

构造一条历史 assistant 消息，正文为 `mermaid` fenced block；断言页面显示源代码，
且不出现 Mermaid SVG。再断言空状态不显示“画一张 Mermaid 流程图说明当前链路”。

- [ ] **Step 2: Run test to verify it fails**

Run: `npm test -- --run frontend/chat/src/App.test.tsx`

Expected: 当前 Mermaid 插件仍渲染图表或空状态仍包含 Mermaid 示例，测试失败。

### Task 2: 移除 Mermaid 能力与依赖

**Files:**
- Modify: `frontend/chat/src/App.tsx`
- Modify: `package.json`
- Modify: `package-lock.json`
- Test: `frontend/chat/src/App.test.tsx`

- [ ] **Step 1: Write minimal implementation**

删除 `@streamdown/mermaid` import；将插件表改为 `{ code }`；删除
`controls.mermaid`；用普通项目结构示例替换 Mermaid 示例；通过
`npm uninstall @streamdown/mermaid` 更新依赖清单和 lockfile。

- [ ] **Step 2: Run focused tests**

Run: `npm test -- --run frontend/chat/src/App.test.tsx`

Expected: PASS。

- [ ] **Step 3: Run frontend verification**

Run: `npm test -- --run`

Run: `npm run typecheck`

Run: `npm run build`

Run: `git diff --check`

Expected: 全部退出码为 0，diff check 无输出。

- [ ] **Step 4: Commit**

只暂存 `App.tsx`、`App.test.tsx`、`package.json`、`package-lock.json`，提交：
`fix: 关闭 Mermaid 流程图渲染`。
