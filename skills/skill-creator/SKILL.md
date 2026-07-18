---
name: skill-creator
description: 创建或改写 BeanAgent Skill（SKILL.md）。当用户要求新建技能、适配现有技能到当前格式或修改技能内容时使用。
when_to_use: 用户要求创建、更新、迁移或校验一个 Skill 时使用。
metadata:
  beanagent:
    always: false
---

# Skill 创建指南

## 目录结构

用户 Skill 必须写入当前运行 workspace：

```text
workspace/skills/<skill-name>/
  SKILL.md
  scripts/       # 可选辅助脚本
  references/    # 可选参考资料
  assets/        # 可选模板或静态资源
```

使用 `write_file` 或 `edit_file` 创建和修改文件。禁止写入源码仓库的内置 `skills/`，禁止写出 workspace。

## SKILL.md 格式

```markdown
---
name: skill-name
description: 一句话说明功能和准确触发场景。
when_to_use: 用户在什么情况下应使用本 Skill。
metadata:
  beanagent:
    always: false
    requires:
      bins: []
      env: []
---

# Skill 标题

正文指令……
```

## 字段规则

- `name` 使用小写字母、数字和连字符，并与目录名一致。
- `description` 同时说明能力和触发条件，避免“通用助手”等宽泛描述。
- `when_to_use` 补充用户意图边界，不重复整段正文。
- `always=true` 只用于每轮都必须生效的短规则；默认保持 `false`。
- `requires.bins` 和 `requires.env` 必须列出实际硬依赖，但不得写入环境变量值。

## 创建流程

1. 确认 Skill 名称、目标行为、触发场景和明确非目标。
2. 检查 `workspace/skills/<name>` 是否已存在；存在时先读取，不能覆盖用户内容。
3. 先写简洁 `SKILL.md`；长参考放入 `references/`，可复用程序放入 `scripts/`。
4. 检查正文引用的相对路径确实存在，并以 Skill Base directory 为基准。
5. 检查 YAML frontmatter、依赖名称和安全边界。
6. 报告创建或修改的文件，并说明该 Skill 会在同一会话的下一轮重新扫描后可用。

## 写作原则

- 只写模型无法从工具描述直接知道的流程和约束。
- 使用命令或输入输出示例代替冗长解释。
- 不把密钥、token、用户数据或绝对机器路径写入 Skill。
- 不宣称不存在的工具、MCP Server 或外部依赖已经可用。
- 修改已有 Skill 时同步清理失效的脚本和引用。
