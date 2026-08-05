"""使用主模型在向量候选中选择受约束的记忆状态动作。"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Literal, Protocol

logger = logging.getLogger(__name__)

MemoryWriteAction = Literal["create", "reinforce", "merge", "supersede", "no_change"]


class DecisionProvider(Protocol):
    async def complete(self, messages: list[dict[str, str]], **kwargs: Any) -> Any: ...


@dataclass(frozen=True, slots=True)
class MemoryWriteDecision:
    action: MemoryWriteAction = "create"
    target_id: str = ""
    merged_summary: str = ""


MEMORY_WRITE_DECISION_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "submit_memory_write_decision",
        "description": "提交候选长期记忆相对已有同类型记忆的状态动作。",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["create", "reinforce", "merge", "supersede", "no_change"]},
                "target_id": {"type": "string"},
                "merged_summary": {"type": "string"},
            },
            "required": ["action", "target_id", "merged_summary"],
            "additionalProperties": False,
        },
    },
}


class MemoryWriteDecider:
    def __init__(self, provider: DecisionProvider) -> None:
        self._provider = provider

    async def decide(
        self,
        memory_type: str,
        summary: str,
        extra: dict[str, object],
        candidates: list[dict[str, object]],
    ) -> MemoryWriteDecision:
        if not candidates:
            return MemoryWriteDecision()
        prompt = _decision_prompt(memory_type, summary, extra, candidates)
        function_name = "submit_memory_write_decision"
        try:
            response = await self._provider.complete(
                [{"role": "user", "content": prompt}],
                tools=[MEMORY_WRITE_DECISION_TOOL],
                tool_choice={"type": "function", "function": {"name": function_name}},
                disable_thinking=True,
            )
            payload = _arguments(response, function_name)
            action = str(payload.get("action") or "create")
            if action not in {"create", "reinforce", "merge", "supersede", "no_change"}:
                return MemoryWriteDecision()
            target_id = str(payload.get("target_id") or "")
            candidate_ids = {str(item.get("id") or "") for item in candidates}
            if action != "create" and target_id not in candidate_ids:
                return MemoryWriteDecision()
            return MemoryWriteDecision(
                action=action,  # type: ignore[arg-type]
                target_id=target_id,
                merged_summary=str(payload.get("merged_summary") or "").strip(),
            )
        except Exception as error:
            logger.warning("记忆写入语义决策失败，保守新增: type=%s error=%s", memory_type, type(error).__name__)
            return MemoryWriteDecision()


def _arguments(response: Any, function_name: str) -> dict[str, Any]:
    calls = getattr(response, "tool_calls", None)
    if not isinstance(calls, list) or len(calls) != 1:
        raise ValueError("记忆决策必须返回一个 tool_call")
    call = calls[0]
    name = call.get("name") if isinstance(call, dict) else getattr(call, "name", "")
    arguments = call.get("arguments") if isinstance(call, dict) else getattr(call, "arguments", None)
    if name != function_name or not isinstance(arguments, dict):
        raise ValueError("记忆决策 tool_call 无效")
    return dict(arguments)


def _decision_prompt(
    memory_type: str,
    summary: str,
    extra: dict[str, object],
    candidates: list[dict[str, object]],
) -> str:
    compact = [{
        "id": item.get("id"),
        "summary": item.get("summary"),
        "happened_at": item.get("happened_at"),
        "extra": item.get("extra_json"),
        "similarity": item.get("vector_score"),
    } for item in candidates]
    type_rules = {
        "event": """### Event（发生过的事件）
- 不同时间、次数、序号、阶段或结果表示不同事件，选择 `create`；不要因表述相似而覆盖。
- 只有确定是同一事件的重复复述，且没有新增事实时，才选择 `reinforce`。
- 只有新记忆明确纠正同一事件的时间、对象或结果时，才选择 `supersede`。
- Event 禁止 `merge`，因为合并可能抹掉事件发生的次数和时间线。

正反例：
- 旧："周一完成第一次部署"；新："周五完成第二次部署" -> `create`，这是不同事件。
- 旧："周一完成第一次部署"；新："周一已完成首次部署" -> `reinforce`。
- 旧："周一完成部署"；新："纠正：实际是周二完成部署" -> `supersede`。""",
        "profile": """### Profile（主体的稳定事实或属性）
- 先确认主体和属性。不同主体即使描述高度相似，也选择 `create`。
- 同一主体、同一单值属性发生变化，且新旧不能同时成立时，可选择 `supersede`。
- 同一主体的多值属性、经历或能力可以同时成立时，选择 `create`；只有需要形成一个完整属性描述时才 `merge`。
- 同一事实的同义复述选择 `reinforce`；新内容已被旧记忆完整包含时选择 `no_change`。

正反例：
- 旧："用户住在北京"；新："用户已经搬到上海" -> `supersede`。
- 旧："用户会 Python"；新："用户也会 Go" -> `create`，两项能力可以同时成立。
- 旧："项目 A 使用 PostgreSQL"；新："项目 B 使用 PostgreSQL" -> `create`，主体不同。""",
        "preference": """### Preference（用户偏好）
- 同一偏好的同义复述选择 `reinforce`；新表达只是旧偏好的子集时选择 `no_change`。
- 两项偏好可以同时成立但属于不同维度时选择 `create`，不要仅因语义接近而合并。
- 同一偏好维度上的兼容补充，只有合并成一条更完整偏好更利于未来使用时才选择 `merge`。
- 只有用户明确反转、否定或纠正同一偏好时，才选择 `supersede`。

正反例：
- 旧："用户喜欢简洁回答"；新："用户偏好简短回答" -> `reinforce`，属于同义复述。
- 旧："用户喜欢简洁回答"；新："用户希望代码附带必要注释" -> `create`，属于不同维度。
- 旧："用户喜欢详细解释"；新："以后不要展开解释，尽量简短" -> `supersede`，属于明确反转。""",
        "procedure": """### Procedure（未来同类场景应遵守的执行规则）
- 先确认适用场景和作用对象；不同项目、工具或场景的规则默认选择 `create`。
- 同一适用场景下，新增步骤、约束或工具要求与旧规则兼容，并且合并后更完整时，可选择 `merge`。
- 同一规则的同义复述选择 `reinforce`；新规则已被旧规则完整覆盖时选择 `no_change`。
- 步骤顺序、必须项、禁止项、约束或工具要求存在冲突时禁止 `merge`。只有新规则明确取代旧规则时才 `supersede`，否则 `create`。

正反例：
- 旧："部署前先运行测试"；新："部署前还要检查回滚方案" -> `merge`，步骤兼容。
- 旧："项目 A 部署必须使用 Docker"；新："项目 B 部署必须使用 Docker" -> `create`，适用场景不同。
- 旧："提交前必须运行 pytest"；新："提交前禁止运行 pytest" -> 禁止 `merge`；明确改规时 `supersede`。""",
    }.get(memory_type, "### 未知类型\n相似不等于重复；无法可靠判断时选择 `create`。")
    return f"""你是长期记忆写入决策代理。判断一条新记忆与已有同类型候选的语义关系，并提交唯一的状态动作。

## 动作定义

- `create`：新记忆是独立事实，或主体、属性、场景、时间、次数、阶段不同；不修改任何旧记忆。
- `reinforce`：新旧是同一事实的同义复述，没有新增信息；增强一条旧记忆。
- `merge`：新旧描述同一主体下的同一属性或同一场景，信息兼容且互补；合并为一条完整记忆。
- `supersede`：新记忆明确纠正、否定或取代一条旧记忆，使旧记忆失效并保留新记忆。
- `no_change`：新记忆提供的信息已被一条旧记忆完整包含，不写入、不增强、不修改。

## 判断顺序

必须依次检查，不得只依据措辞或相似度：
1. 主体是否相同；不同主体默认 `create`。
2. 属性或场景是否相同；不同属性、项目、工具或场景默认 `create`。
3. 时间、次数或阶段是否相同；不同时间点发生的相似事件可以同时存在。
4. 新旧内容能否同时成立；能够同时成立通常不是 `supersede`。
5. 新记忆是否提供新增有效信息；没有新增信息才考虑 `reinforce` 或 `no_change`。

候选的相似度只用于召回候选，不是重复、合并或冲突的证据。证据不足、多个候选关系不确定或无法唯一选择目标时，保守选择 `create`。

## 当前类型规则与正反例

{type_rules}

## 输出约束

- 必须且只能调用 `submit_memory_write_decision` 一次，不得输出普通正文。
- `reinforce`、`merge`、`supersede`、`no_change` 必须从候选中选择唯一 `target_id`；`create` 的 `target_id` 必须为空字符串。
- 只有 `merge` 填写 `merged_summary`；其他动作必须返回空字符串。
- `merged_summary` 必须是可独立理解的完整记忆，并保留新旧双方所有仍然有效的信息；不得丢失主体、时间、步骤、约束或工具要求。
- 一次决策最多影响一条候选；不得假设未提供的事实。

## 待判断内容

记忆类型：{memory_type}
新记忆：{json.dumps({'summary': summary, 'extra': extra}, ensure_ascii=False)}
同类型候选：{json.dumps(compact, ensure_ascii=False)}"""


__all__ = ["MemoryWriteDecider", "MemoryWriteDecision"]
