# BeanAgent 对齐 Akashic 记忆机制设计

## 1. 背景与目标

BeanAgent 已具备 SQLite + sqlite-vec 向量存储、历史事实与流程偏好双查询改写、向量和关键词双路检索、RRF 融合、窗口期 consolidation、PENDING 缓冲、语义替代以及回复后旧规则失效等基础能力。本设计在保持现有技术栈和模块边界的前提下，补齐 akashic-agent `memory2/` 与 `core/memory/markdown.py` 中已经验证的关键行为。

目标数据流如下：

```text
当前消息与近期对话
  -> 记忆检索 Gate 与双 query 改写
  -> 原始 query、改写 query 和可选 HyDE 多路召回
  -> 向量检索 + 关键词检索 + RRF 融合
  -> 类型阈值、规则适用性和注入配额规划
  -> 注入 Agent 回复上下文
  -> TurnCommitted 后窗口期结构化提炼
  -> new / reinforce / merge / supersede / ignore 写入决策
  -> PENDING 缓冲及回复后旧规则失效
```

## 2. 范围与非目标

### 2.1 范围

- 存储继续使用 SQLite + sqlite-vec，运行数据继续写入 workspace 下的 `memory/`。
- QueryRewriter 使用真实近期对话，独立生成 episodic query 和 procedure query。
- 原始 query 与改写 query 共同参与多 query 检索；可选 HyDE 只能扩充结果，不能覆盖原始召回。
- Retriever 保持向量与关键词双路召回、局部失败隔离和加权 RRF，并增加类型阈值、注入配额及可审计 trace。
- 写入阶段引入独立去重决策，统一表达新增、强化、合并、替代和忽略。
- procedure 使用结构化规则判断适用性、增量更新和冲突。
- consolidation 窗口统一提取 event、profile、preference、procedure 和 pending 候选。
- 回复后异步处理用户对既有 procedure/preference 的明确否定。

### 2.2 非目标

- 不引入 PostgreSQL、pgvector 或新的外部数据库服务。
- 不复制 akashic-agent 的插件、MCP、主动任务或多渠道架构。
- 不设计覆盖全部记忆类型的 TTL、自动过期扫描或复杂知识图谱。
- 不以 LLM 判断替代确定性的内容哈希、作用域过滤和数据库事务。
- 不在单轮回复的关键路径同步执行 consolidation、隐式写入或失效维护。

## 3. 组件设计

### 3.1 检索决策与查询改写

`MemoryEngine.retrieve_for_turn()` 从 SessionStore 加载当前 session 的近期消息，构造成受长度限制的文本并交给 `QueryRewriter.decide()`。QueryRewriter 继续并行执行两个轻量模型请求：

- history lane 判断是否需要历史事实，并生成 `episodic_query`。
- procedure lane 始终尝试把当前请求改写成可命中 preference/procedure summary 的查询。

任一路失败不得吞掉另一路结果。history lane 无效或超时时维持 fail-open，使用当前用户消息作为 episodic query；procedure lane 失败时仅丢弃该辅助查询。

procedure/preference 检索必须同时保留原始用户消息和改写查询。这样既能命中原文关键词，也能命中规范化的长期规则摘要。

### 3.2 多查询、HyDE 与召回充分性

Retriever 接受一个主 query 和多个 `aux_queries`，对去重后的文本批量生成 embedding，再复用现有批量向量搜索。关键词 lane 以所有查询提取的词项并集执行一次检索。

新增 `HyDEEnhancer` 作为可选增强器：

1. 先执行原始多 query 召回。
2. 仅当召回为空或充分性检查认为证据不足时，请轻量模型生成假设性记忆摘要。
3. 使用假设摘要执行第二次召回。
4. 按记忆 ID 做 union dedup，完整保留原始结果及分数，只追加原始结果中不存在的 HyDE 命中。
5. 生成、embedding 或检索失败时返回原始结果。

`SufficiencyChecker` 只判断当前结果是否值得触发增强，不直接决定最终答案。首版采用确定性规则：存在达到类型阈值的命中即认为充分；LLM 充分性判断不在本阶段引入。

### 3.3 RRF、类型阈值与注入规划

Retriever 继续通过加权 RRF 融合 vector lane 和 keyword lane。RRF 仅决定跨 lane 顺序，不能覆盖原始相似度等执行阈值所需信号。

新增 `InjectionPlanner`，按以下顺序生成最终注入列表：

1. 排除 superseded 条目和作用域不匹配条目。
2. 按 memory type 应用最低分数阈值。
3. procedure 通过结构化规则适用性校验。
4. procedure/preference 与 event/profile 分别应用注入配额。
5. 保持 RRF 顺序，不在规划器内重新发起检索。

默认配额与阈值进入 `MemoryRetrievalConfig`，允许通过 TOML 覆盖。trace 至少记录原始 query、辅助 query、是否使用 HyDE、各 lane 命中 ID、淘汰原因和最终注入 ID。

### 3.4 去重决策与写入

新增 `DedupDecision` 与 `DedupDecider`。决策枚举为：

- `new`：没有可信重复或冲突，新增活动条目。
- `reinforce`：事实或规则等价，只增加 reinforcement。
- `merge`：候选是同一事实或规则的增量补充，原子更新目标条目。
- `supersede`：候选与旧条目同主题且互斥，退休旧条目后写入新条目。
- `ignore`：候选价值不足、类型错误或证据不明确。

决策顺序固定为：内容哈希精确去重、同类型向量候选召回、确定性类型规则、必要时轻量 LLM 判断。LLM 输出只能引用候选集中存在的 ID；无效输出和模型异常默认降级为 `new`，不得误删旧记忆。

SQLite 写入继续由 `MemoryStore2` 的锁保护。reinforce、merge 和 supersede 必须在存储层提供原子操作，避免并发 Turn 产生多个活动版本。

### 3.5 Procedure 结构化规则

procedure 的 `extra_json` 增加 `rule_schema`：

```json
{
  "scenarios": ["处理外部视频链接"],
  "triggers": ["用户发送视频链接并要求处理"],
  "tools": ["web_fetch"],
  "steps": ["读取链接内容", "按用户目标处理"],
  "forbidden": []
}
```

确定性冲突规则：

- 场景无交集时允许并存。
- 场景相同且执行要求等价时 reinforce。
- 新步骤是旧步骤的严格扩展时 merge。
- 同一场景下 tools、steps 或 forbidden 存在直接互斥时 supersede。
- 缺少可执行约束的 procedure 降级为 preference。

规则 schema 既用于写入冲突判断，也用于召回后的适用性过滤，不要求引入新的工具注册体系。

### 3.6 窗口期结构化提炼与 PENDING

Consolidator 保持按 `last_consolidated` 游标选择旧消息窗口，并在达到阈值后一次性调用提取器。提取结果扩展为：

- `history_entries`：有时间语义的历史事件。
- `profile_items`：用户本人或稳定客观处境。
- `preference_items`：用户明确表达的长期偏好、要求或禁忌。
- `procedure_items`：Agent 在可复用场景下应遵守的执行规则。
- `pending_items`：适合进入可审阅 Markdown 长期记忆、但不直接作为向量条目的候选。
- `recent_context`：下一轮 consolidation 可复用的近期压缩上下文。

同一窗口内先按规范化文本与类型去重，再交给 Memorizer。向量写入失败时保留 consolidation outbox，启动后可幂等重放。PENDING 继续使用 `source_ref + kind` 幂等写入、原子快照、失败回滚和启动恢复，不改成新的数据库层。

### 3.7 回复后旧规则失效

`PostResponseMemoryWorker` 继续在 `TurnCommitted` 后的后台队列运行：

1. 从用户消息中提取被明确否定的行为主题。
2. 仅召回相关 procedure 和 preference。
3. 排除本轮显式 memorize 新写入的受保护 ID。
4. 让轻量模型从候选 ID 中选择应 supersede 的旧条目。
5. 软删除条目并发布 `MemoryWritten(action="supersede")`。

模型异常、token budget 不足或无合法 ID 时不做变更。该 worker 不处理 event/profile 的通用过期。

## 4. 错误处理与降级

- Query rewrite 超时：history 使用原始消息，procedure 为空。
- 批量 embedding 失败：按查询逐条补救；全部失败时保留关键词结果。
- vector lane 超时：取消未完成任务并返回 keyword lane 可用结果。
- HyDE 任一步失败：返回未经修改的原始结果。
- Dedup LLM 失败或输出非法 ID：默认新增，不 supersede 现有条目。
- consolidation 或隐式写入失败：保留 outbox，不推进不可恢复状态。
- post-response invalidation 失败：记录日志，不影响已经发送的回复。
- 所有后台队列在关闭时 drain，关闭入口保持幂等。

## 5. 实施拆分与提交边界

1. `docs: 设计对齐Akashic的记忆机制`
2. `feature: 记忆检索使用近期历史与多查询`
3. `feature: 增加记忆HyDE召回增强`
4. `feature: 增加记忆注入规划`
5. `feature: 增加记忆去重决策器`
6. `feature: 增加流程记忆规则冲突处理`
7. `feature: 完善窗口期结构化记忆提取`
8. `feature: 完善回复后旧记忆失效保护`
9. `test: 固化记忆系统离线闭环`

每个功能提交同时包含对应测试，不创建重复汇总提交。若某项能力经检索确认已完整存在，则只补缺失测试或跳过该提交，不为追求提交数量制造无效改动。

## 6. 验收标准

- 指代型问题的 query rewrite 能读取对应 session 的近期历史。
- 原始 query、episodic query 和 procedure query 去重后参与召回。
- HyDE 失败时原始召回结果、顺序和分数不丢失。
- 向量与关键词结果通过 RRF 融合；embedding 全部失败时仍返回关键词结果。
- 类型阈值和注入配额不会让 event/profile 挤占全部 procedure/preference 空间。
- 完全重复写入只增加 reinforcement，不产生第二个 active 条目。
- procedure 增量规则执行 merge，直接冲突规则执行 supersede。
- 窗口期能够一次性提取 event、profile、preference、procedure 和 pending。
- consolidation 重放、PENDING 快照回滚与启动恢复均保持幂等。
- 用户明确否定旧规则后，相关 procedure/preference 默认召回不可见。
- L1 覆盖新增纯逻辑和组件；L2 覆盖 SQLite、Session、后台队列协作；L3 使用 Fake LLM 验证 WebSocket 到下一轮记忆加载的闭环。

## 7. 参考位置

BeanAgent 事实来源：

- `memory/engine.py`
- `memory/query_rewriter.py`
- `memory/retriever.py`
- `memory/memorizer.py`
- `memory/consolidator.py`
- `memory/post_response_worker.py`
- `memory/store.py`
- `tests/unit/memory/`

akashic-agent 只读参考：

- `D:\akashic-agent\memory2\query_rewriter.py`
- `D:\akashic-agent\memory2\query_builder.py`
- `D:\akashic-agent\memory2\hyde_enhancer.py`
- `D:\akashic-agent\memory2\sufficiency_checker.py`
- `D:\akashic-agent\memory2\injection_planner.py`
- `D:\akashic-agent\memory2\dedup_decider.py`
- `D:\akashic-agent\memory2\rule_schema.py`
- `D:\akashic-agent\memory2\post_response_worker.py`
- `D:\akashic-agent\core\memory\markdown.py`

