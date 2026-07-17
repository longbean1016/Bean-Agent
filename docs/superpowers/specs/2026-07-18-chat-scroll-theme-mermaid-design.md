# 聊天滚动、主题与 Mermaid 防线设计

## 范围

本需求包含三个可独立验证和提交的前端点：允许用户在流式输出期间向上阅读；在右上角
增加浅色、跟随系统、深色三态主题；把 Mermaid fenced block 在进入 Markdown 渲染器
前确定性改写为普通文本代码块。三点不改变后端协议、Session 数据或其他 Markdown 行为。

## 流式滚动

会话区域根据距底部的距离维护 `isAtBottom`。用户离开底部后，流式 delta 不再调用
`scrollTo`；重新滚到底部、发送消息或切换会话时恢复跟随。离开底部时显示固定在会话
视口右下方的向下箭头按钮，点击后平滑回到底部并恢复跟随。

## 三态主题

“已连接”右侧放置由 Sun、Monitor、Moon 图标组成的三段控件。选择保存到
`localStorage`；默认 `system`。系统模式监听 `prefers-color-scheme: dark`，并把实际
结果写入 `document.documentElement.dataset.theme`。桌面显示文字，移动端只显示图标。
所有主要界面颜色使用语义 CSS 变量，确保浅色和深色状态完整覆盖。

## Mermaid 防线

源码已移除 Mermaid 渲染插件，但为防止旧配置或未来插件重新识别，在消息预处理阶段
把 fenced block 的 info string `mermaid` 改为 `text`。仅处理 fence 起始行，不修改
代码正文。生成的新 bundle 使用内容 hash；浏览器验收时需硬刷新清除旧 bundle。

## 提交边界

1. `fix: 允许流式输出时向上滚动`
2. `feature: 增加三态主题切换`
3. `fix: 强制 Mermaid 以文本代码显示`

每个提交含直接相关组件测试；不运行 Playwright 端到端测试。
