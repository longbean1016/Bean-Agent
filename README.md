<div align="center">

# BeanAgent

### 🤖 在记忆与实践中，成为更懂你的个人助手

<p><em>从聊天对话与任务实践中提炼可复用的记忆<br>理解文本、图片等多模态内容，并通过工具与技能协助完成实际任务。</em></p>

![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.134%2B-009688?logo=fastapi&logoColor=white)
![React](https://img.shields.io/badge/React-19-61DAFB?logo=react&logoColor=20232A)
![TypeScript](https://img.shields.io/badge/TypeScript-5.9-3178C6?logo=typescript&logoColor=white)

</div>

---

<div align="center">

[🎯 项目介绍](#-项目介绍) · [⚡ 并发与排队](#-多会话并发与排队) · [🔔 主动能力](#-主动提醒与主动聊天) · [🚀 快速开始](#-快速开始) · [📁 项目结构](#-项目结构)

</div>

## 🎯 项目介绍

BeanAgent 是一个以 **Web 对话为入口**、围绕本地工作区运行的个人 AI Agent。它通过 FastAPI 与 WebSocket 建立实时通信，由 AgentLoop 和 Pipeline 驱动模型推理、工具调用与续轮执行，并将会话历史和长期记忆持久化到用户工作区。

项目提供完整的前后端闭环，同时保持核心组件可替换：LLM Provider、Session、Memory、Tools 和 Skills 均通过清晰边界组装，便于继续扩展模型、工具与记忆策略。

- 💬 **实时 Web Chat**：支持流式回复、历史会话与附件上传
- 🔁 **ReAct 工具闭环**：支持单次、多次及续轮工具调用
- 🧠 **长期记忆**：支持向量检索、融合排序、去重与后台整理
- 🗂️ **Session 持久化**：使用 SQLite 存储会话历史，并保证同一会话的写入顺序
- 👁️ **多模态理解**：支持主模型直接理解图片，或接入独立视觉模型
- 🛠️ **内置工具**：提供文件、Shell、Web、视觉与记忆等工具
- 🧩 **Skills 扩展**：支持内置与工作区 Skill 的分层发现和加载
- ⚡ **多会话并发**：默认同时处理 5 个会话，额外 20 个会话按先进先出顺序排队，并实时展示队列位置
- 🔔 **主动提醒与主动聊天**：按会话配置开关、主动程度、发送频率与勿扰时间，支持 WebSocket 推送和离线补发

## ⚡ 多会话并发与排队

不同会话可以同时生成回复，同一会话仍按顺序处理，当前任务结束前不能重复提交。默认并发与排队容量可在 `config.toml` 中调整：

```toml
[agent]
max_concurrent_turns = 5
max_queued_turns = 20
```

并发达到上限后，新任务按先进先出顺序进入内存队列。前端会根据位置显示 `排队中 · 即将开始` 或 `排队中 · 前面还有 N 个会话`；超过队列容量时提示 `当前任务较多，请稍后再试`。排队中和生成中的任务均可停止。

## 🔔 主动提醒与主动聊天

在会话列表对应会话的 `...` 菜单中选择“主动设置”。提醒与主动聊天相互独立，可以分别开启：

- **提醒**：由正常对话中的提醒工具创建，Web 设置页负责开关、勿扰策略和已有提醒管理，不另设创建表单
- **主动聊天**：：Agent 综合最近会话、相关兴趣记忆和必要的只读工具信息，结合主动程度判断是否发起对话；“克制、均衡、积极”表示算法倾向，保证固定次数。
- **发送约束**：可设置主动聊天的最短间隔、每日最多次数，以及提醒和主动聊天共用的勿扰时段
- **勿扰策略**：提醒进入勿扰时段后可选择延后发送、照常发送或跳过；主动聊天在勿扰时段不会发起
- **消息投递**：网页在线时通过 WebSocket 实时推送；暂时离线时保存待投递通知，在对应会话重新连接后补发

提醒消息会标记来源和原定时间；固定提醒不写成普通的用户/助手对话，主动聊天则进入正常会话历史。普通问题的正常回复不会被重复包装成提醒或主动聊天。

## 🚀 快速开始

### 1. 环境要求

- Python 3.11+
- Node.js 20+ 与 npm
- 一个 OpenAI 兼容的 LLM API
- 启用长期记忆时需要 Embedding API

### 2. 安装项目依赖

#### Windows PowerShell

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
npm install
```

#### macOS / Linux

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
npm install
```

### 3. 创建配置

复制配置模板：

```powershell
Copy-Item config.example.toml config.toml
```

macOS / Linux 使用：

```bash
cp config.example.toml config.toml
```

编辑 `config.toml`，选择模型并配置 API Key。配置模板默认使用环境变量占位符，PowerShell 可通过以下方式设置：

```powershell
$env:DEEPSEEK_API_KEY = "your-api-key"
$env:DASHSCOPE_API_KEY = "your-embedding-key"
```

macOS / Linux 使用：

```bash
export DEEPSEEK_API_KEY="your-api-key"
export DASHSCOPE_API_KEY="your-embedding-key"
```

> [!TIP]
> 不需要长期记忆时，可将 `[memory]` 中的 `enabled` 设置为 `false`，此时无需配置 Embedding 服务。

### 4. 配置视觉能力（可选）

BeanAgent 支持两种图片理解方式，可根据所使用的模型选择其中一种。

#### 方式一：主模型直接接收图片

当主模型本身支持多模态输入时，开启 `multimodal`：

```toml
[llm]
multimodal = true
```

上传的图片会随对话内容直接发送给主模型，无需额外配置视觉模型。

#### 方式二：配置独立视觉模型

当主模型不支持图片时，保持 `multimodal = false`，并启用 `[llm.vl]`。Agent 会通过视觉工具读取上传的图片：

```toml
[llm]
multimodal = false

[llm.vl]
provider = "qwen"
model = "qwen-vl-max"
api_key = "${DASHSCOPE_API_KEY}"
base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
max_tokens = 2048
request_timeout_s = 90.0
```

> [!IMPORTANT]
> 不要将不支持图片的主模型配置为 `multimodal = true`。独立视觉模型仅负责图片理解，主对话和工具编排仍由 `[llm]` 中的模型完成。

### 5. 配置并发容量（可选）

默认配置适合本地使用。如需根据模型服务的限流或机器资源调整，可修改：

```toml
[agent]
max_concurrent_turns = 5
max_queued_turns = 20
```

`max_concurrent_turns` 是同时生成回复的不同会话数量，`max_queued_turns` 是并发占满后允许等待的任务数量。

### 6. 构建前端并启动

```powershell
npm run build
python main.py
```

启动成功后访问：<http://127.0.0.1:6322>

运行数据默认保存在 `~/.beanagent/workspace`。需要自定义配置或工作区时：

```powershell
python main.py --config config.toml --workspace D:\BeanAgentWorkspace
```

### 7. 前端开发模式

开发时分别启动后端与 Vite。Vite 会自动代理 `/api` 和 `/ws`。

终端 1：

```powershell
python main.py
```

终端 2：

```powershell
npm run dev
```

### 8. 常用验证命令

后端测试：

```powershell
pytest tests/unit -q
pytest tests/integration -q
pytest tests/e2e/backend -q
```

前端检查：

```powershell
npm run typecheck
npm test
npm run build
```

## 📁 项目结构

```text
BeanAgent/
├── agent/               # Agent 核心、Provider、配置、消息与事件总线
├── bootstrap/           # 应用依赖组装与生命周期管理
├── frontend/chat/       # React + Vite 聊天前端
├── memory/              # 长期记忆、检索、去重与优化
├── proactive/           # 主动提醒、主动聊天、定时调度与离线投递
├── session/             # Session 管理与 SQLite 存储
├── skills/              # 内置 Skill 定义
├── tools/               # 内置工具及注册表
├── tests/
│   ├── unit/            # 单元测试
│   ├── integration/     # 本地组件集成测试
│   └── e2e/             # 前后端端到端测试
├── config.example.toml  # 配置模板
├── main.py              # 服务启动入口
├── requirements.txt     # Python 运行依赖
└── package.json         # 前端依赖与脚本
```

<div align="center">

**BeanAgent · 在记忆与实践中，成为更懂你的个人助手**

</div>
