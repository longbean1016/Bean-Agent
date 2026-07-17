# 图片能力分流设计

## 目标

对齐 Akashic 的图片分流语义：主模型支持多模态时直接接收图片；主模型不支持
多模态但配置独立 VL 时，通过 `read_image_vision` 工具识图；两者都不可用时明确
说明能力边界。DeepSeek Provider 的图片过滤继续作为末端保护，不承担能力路由。

## 数据流

`WebChannel` 继续把已验证的上传路径放入 `InboundMessage.media`。Pipeline 构建当前
用户消息时根据组装阶段注入的两个稳定能力标志分流：

1. `multimodal=True`：本地图片编码为 OpenAI 兼容 `image_url`，非图片附件保留路径。
2. `multimodal=False` 且 `vl_available=True`：不生成任何 `image_url`；在文本中列出
   图片路径，并明确提示模型调用 `read_image_vision(path=..., prompt=...)`。
3. 两者均为 `False`：不生成 `image_url`；保留附件路径并说明当前无法识图。

`vl_available` 只在独立视觉 Provider 已成功构造且配置了 VL 模型时为真。Pipeline
不直接调用 VL，仍由主模型通过现有 ReAct 工具链调用，保持工具事件、tool_chain 和
错误降级行为一致。

## 修改边界

- `agent/attachment_content.py`：集中实现三分支消息构造。
- `agent/pipeline.py`：构造函数接收并保存能力标志，处理 Turn 时传给附件构造函数。
- `bootstrap/app.py`：从 `Config` 和已构造的视觉 Provider 注入真实能力。
- 单元测试覆盖三条分支及 Runtime 注入；不修改上传、Provider、视觉工具和前端。

## 验收

- DeepSeek + 独立 Qwen VL 的当前消息中没有 `image_url`，但含图片路径和
  `read_image_vision` 提示。
- 多模态主模型仍收到图片块。
- 无视觉能力时不会诱导调用不存在的工具。
- 只运行相关单元测试；端到端测试由用户执行。
