# Akashic Scroll and Mermaid Compatibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 对齐 akashic 的可中断自动滚动和 Mermaid 展示，并保证会话切换到底部及图表非交互、尺寸稳定。

**Architecture:** 使用 `use-stick-to-bottom` 统一拥有滚动状态，将滚动视口与内容布局分层；使用 Streamdown Mermaid 插件恢复 SVG，并通过外层 CSS 和事件边界禁止缩放拖动。后端协议与消息数据保持不变。

**Tech Stack:** React 19、TypeScript、Streamdown、use-stick-to-bottom、Mermaid、Vitest、CSS。

---

### Task 1: 会话滚动与底部定位

**Files:**
- Modify: `frontend/chat/src/App.tsx`
- Modify: `frontend/chat/src/styles.css`
- Test: `frontend/chat/src/App.test.tsx`
- Modify: `package.json`
- Modify: `package-lock.json`

- [ ] 新增失败测试：切换到历史会话后在消息渲染完成时强制滚到底部。
- [ ] 新增失败测试：流式期间向上滚动后保持阅读位置，点击按钮或发送消息恢复跟随。
- [ ] 引入 `use-stick-to-bottom`，以 `Conversation`/`ConversationContent` 两层替换手写距离状态。
- [ ] 会话切换使用一次性强制到底部信号，避免旧会话滚动事件覆盖新会话意图。
- [ ] 运行聚焦测试、类型检查和 `git diff --check`，提交 `fix: 对齐会话滚动与底部定位`。

### Task 2: 受限 Mermaid 流程图

**Files:**
- Modify: `frontend/chat/src/App.tsx`
- Modify: `frontend/chat/src/styles.css`
- Test: `frontend/chat/src/App.test.tsx`
- Modify: `package.json`
- Modify: `package-lock.json`

- [ ] 将现有 Mermaid 文本化测试改为失败测试：闭合 fence 生成图表而非普通代码块。
- [ ] 安装并注册 `@streamdown/mermaid`，删除 `mermaid -> text` 输入改写。
- [ ] 为 Mermaid 容器和 SVG 增加宽高、overflow、pointer、touch 与固定图标尺寸约束。
- [ ] 验证普通回答与思考内容采用相同插件策略，渲染失败不破坏消息列表。
- [ ] 运行完整前端单测、类型检查、生产构建和 `git diff --check`，提交 `feature: 恢复受限的 Mermaid 流程图`。

### Task 3: 固定预览与大图查看

**Files:**
- Modify: `frontend/chat/src/App.tsx`
- Modify: `frontend/chat/src/styles.css`
- Test: `frontend/chat/src/App.test.tsx`

- [ ] 新增失败测试：内嵌 Mermaid 提供“查看大图”，不显示缩放按钮。
- [ ] 新增失败测试：点击后出现接近全屏查看器及缩放、复位按钮，关闭后返回消息。
- [ ] 配置 Streamdown fullscreen control，使用作用域 CSS 只禁用内嵌画布交互，不影响 Portal 内全屏画布。
- [ ] 将内嵌预览固定在 380px 高度，增加边框、背景、标题栏和稳定图标尺寸。
- [ ] 为延迟渲染增加“正在生成流程图”骨架；通过 Mermaid error component 提供中文错误、重试、查看与复制源码。
- [ ] 运行完整前端单测、类型检查、生产构建和 `git diff --check`，提交 `feature: 增加 Mermaid 大图查看器`。
