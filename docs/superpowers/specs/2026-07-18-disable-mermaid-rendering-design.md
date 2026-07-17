# 关闭 Mermaid 渲染设计

## 目标

关闭聊天消息中的 Mermaid SVG 和交互式图表渲染，避免复杂流程图撑大消息区域，
以及鼠标进入图表区域后触发缩放、拖动或尺寸重算。Mermaid 源码仍作为普通代码块
显示并支持复制。

## 根因

BeanAgent 把 `mermaid` 注册进 Streamdown 插件表。`controls.mermaid = false` 只隐藏
控件，不会禁用插件，因此 fenced `mermaid` 内容仍被转换为交互式 SVG。通用
`svg { max-width: 100% }` 无法消除插件内部视口和交互逻辑造成的布局问题。

## 设计

- 从聊天 Markdown 插件表移除 `mermaid`，只保留现有代码高亮插件。
- 删除 Mermaid import、无效的 Mermaid controls 配置和 npm 直接依赖。
- fenced `mermaid` 由 Streamdown 回退为普通代码块，不改写消息正文。
- 移除空会话中的“画一张 Mermaid 流程图”示例，避免诱导生成不再预览的内容。
- 不修改其他 Markdown、代码复制、链接、表格或附件行为。

## 验收

- 历史消息包含 Mermaid fenced block 时能看到源码和 `mermaid` 语言标签。
- 页面中不出现 Mermaid 生成的 SVG 或交互图表。
- 空会话不再展示 Mermaid 示例。
- 前端单元测试、类型检查和生产构建通过。
