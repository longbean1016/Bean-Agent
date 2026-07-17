# Image Routing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 对齐 Akashic，根据主模型多模态能力和独立 VL 可用性正确分流上传图片。

**Architecture:** 附件内容构造函数是唯一分流点；Pipeline 只持有应用启动时注入的能力标志，Bootstrap 负责从配置和实际 Provider 推导这些标志。DeepSeekStrategy 保留为协议末端保护。

**Tech Stack:** Python 3.11、pytest、现有 OpenAI 兼容 Provider 与 ReAct ToolRegistry。

---

### Task 1: 附件消息三分支

**Files:**
- Modify: `agent/attachment_content.py`
- Test: `tests/unit/agent/test_pipeline.py`

- [ ] **Step 1: Write the failing tests**

新增测试分别断言：多模态分支包含 `image_url`；独立 VL 分支只包含图片路径和
`read_image_vision` 提示；无视觉能力分支包含不可识图说明且不包含工具提示。

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/agent/test_pipeline.py -q`

Expected: 新增的独立 VL 分支测试因当前函数无能力参数、仍返回 `image_url` 而失败。

- [ ] **Step 3: Write minimal implementation**

为 `build_current_user_content()` 增加 keyword-only 的 `multimodal`、`vl_available`
参数。非多模态时按 Akashic 格式生成媒体引用和工具提示，不读取或编码图片内容。

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/agent/test_pipeline.py -q`

Expected: PASS。

### Task 2: Pipeline 与 Bootstrap 能力注入

**Files:**
- Modify: `agent/pipeline.py`
- Modify: `bootstrap/app.py`
- Test: `tests/unit/agent/test_pipeline.py`
- Test: `tests/unit/bootstrap/test_app.py`

- [ ] **Step 1: Write the failing tests**

新增 Pipeline 行为测试，验证 `multimodal=False, vl_available=True` 时传给 Provider 的
当前消息不含 `image_url`；新增 Runtime 测试验证配置值被注入 Pipeline。

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/agent/test_pipeline.py tests/unit/bootstrap/test_app.py -q`

Expected: Pipeline 构造函数不接受能力参数或当前消息仍含图片块。

- [ ] **Step 3: Write minimal implementation**

Pipeline 构造函数保存两个布尔值并传给附件构造函数；Bootstrap 注入
`config.llm.multimodal` 与 `vision_provider is not None`。

- [ ] **Step 4: Run tests and static diff checks**

Run: `python -m pytest tests/unit/agent/test_pipeline.py tests/unit/bootstrap/test_app.py tests/unit/tools/test_vision.py -q`

Run: `git diff --check`

Expected: 全部 PASS，且 diff check 无输出。

- [ ] **Step 5: Commit**

只暂存上述实现和测试文件，提交：`fix: 对齐图片能力分流`。
