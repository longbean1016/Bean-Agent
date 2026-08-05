"""从 Consolidation 对话窗口提取稳定的隐式长期记忆。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from memory.structured_output import (
    IMPLICIT_MEMORY_TOOL,
    complete_forced_function,
)


@dataclass(slots=True)
class ImplicitMemoryDraft:
    profile: list[dict[str, object]] = field(default_factory=list)
    preference: list[dict[str, object]] = field(default_factory=list)
    procedure: list[dict[str, object]] = field(default_factory=list)


class ProviderApi(Protocol):
    async def complete(
        self,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]] | None = None,
        *,
        tool_choice: str | dict[str, Any] = "auto",
        disable_thinking: bool = False,
    ) -> Any: ...


class ImplicitLongTermExtractor:
    """对齐参考实现，只在较长 Consolidation 窗口中提取新长期事实。"""

    def __init__(self, provider: ProviderApi) -> None:
        self._provider = provider

    async def extract(self, conversation: str, existing_profile: str = "") -> ImplicitMemoryDraft:
        data = await complete_forced_function(
            self._provider,
            _build_prompt(conversation, existing_profile),
            IMPLICIT_MEMORY_TOOL,
            required_arrays=("profile", "preference", "procedure"),
        )
        return ImplicitMemoryDraft(
            profile=_items(data.get("profile")),
            preference=_items(data.get("preference")),
            procedure=_items(data.get("procedure")),
        )


def _items(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict) and str(item.get("summary") or "").strip()]


def _build_prompt(conversation: str, existing_profile: str) -> str:
    # 先约束证据来源和材料主体，再判断长期价值并评分情绪；顺序不能倒置，
    # 否则截图中的第一人称或强烈但短暂的情绪容易被误写为用户长期记忆。
    return f"""你是长期记忆提取专家。从对话窗口中一次性提取三类长期记忆，返回 JSON。

默认答案是所有数组为空。提取门槛要高，宁可不提取，也不要把临时信息写进长期记忆。

【核心判断标准】
把这条信息放进 6 个月后的一次全新对话，它还有用吗？
是：可能是长期记忆，继续检查。否：不是长期记忆，留空。

【三类记忆共同准入规则】
以下规则同时适用于 profile、preference 和 procedure；后面的检查 0、A、B、C 以及材料主体归属规则继续有效：
- 只有 USER 在当前对话中直接陈述，或明确确认的内容才允许提取。
- ASSISTANT 的建议、解释、复述、举例、推测和工具结果不能作为记忆来源。
- USER 没有反驳、默认接受或继续追问，不等于明确授权。
- 用户提问、追问、反问、记忆测试句不算事实披露，绝对禁止反推。

【三类记忆的语义】
profile：关于用户本人、关系人或其客观处境的稳定事实。回答“用户是谁、有什么、长期处于什么状态”。category 只能是 personal_fact、purchase、decision、status。
preference：USER 明确表达且可能跨 session 稳定成立的喜欢、厌恶、选择倾向或服务偏好，包括回答方式、推荐内容、住宿、游戏、饮食等领域的稳定偏好。回答“用户喜欢什么、避免什么、希望怎样被推荐或服务”，而不是未来必须执行的规则。“希望、请、最好、倾向于”等表达服务方式或内容取向时，通常仍是 preference；即使句子中出现“以后”，也不能仅凭该词改判为 procedure。
procedure：USER 明确提出或确认的、agent 在未来特定场景下应遵守的可复用执行规则。必须同时能看出适用场景和执行要求；只有“必须、禁止、先……再……、每次遇到……都……”等强制、顺序或触发语义，才优先判为 procedure。单纯表达回答、讲解、推荐或沟通风格的偏好，不因出现“以后”就改判为 procedure。tool_requirement 只有 USER 明确要求某个工具时才填写，没有工具要求不代表不能是 procedure。

【类型判定顺序】
对 USER 消息中的每个独立事实分别判断，不要把一整句话强行归为一个类型：
1. 一次具体行动、结果、时间点、次数、阶段或临时安排，属于 Event 路径；不得因为“完成、放弃、里程碑、当前”等词写成长期记忆。
2. 未来场景下必须遵守的动作、步骤、顺序、禁止项或工具要求，判为 procedure；只有表达强制执行或明确触发规则时才适用。
3. 长期喜欢、避免、选择或希望被服务的方式，判为 preference；表达服务风格的“以后请/希望/最好”仍优先判为 preference。
4. 用户身份、拥有物、关系、长期经历或稳定处境，判为 profile。
如果同一句同时包含稳定事实和一次性事件，按最小事实单元拆分：只把稳定事实写入对应长期记忆，一次性事件由 history_entries 路径处理；不要把同一个意思重复写成多个长期类型。

【类型判断的泛化约束】
- 上述判断依据是主张的主体、适用范围、持续性和执行强度，不是固定关键词的机械匹配。
- “今天、现在、长期、完成、喜欢、以后”等词只能作为辅助信号；必须结合整句语义判断，不能单凭一个词决定类型。
- 同一套规则适用于工作、学习、项目、设备、住宿、出行、内容和生活等不同领域。
- 表达“是什么/有什么/处于什么稳定状态”通常是 profile；表达“喜欢/避免/倾向于什么”通常是 preference；表达“未来在什么场景必须怎么做”通常是 procedure；表达“某次发生了什么”通常由 Event 路径处理。
- 稳定偏好与临时例外同时出现时，分别处理：保留 USER 明确表达的稳定 preference，临时例外由 Event 路径处理，不得因为例外删除或改写稳定偏好。
- 一条 USER 消息明确提到多个主体或多项事实时，每个主体和事实分别判断并保留关系归属；不得因为其中一项更突出而丢弃另一项直接陈述。

【Event 与长期记忆边界】
本提取器不输出 Event。以下内容属于 Event，不属于 profile、preference 或 procedure：
- 用户某次具体行动、经历或结果；
- 带有今天、昨天、刚刚、这次、首次、第二次等时间或次数的事实；
- 一次性部署、发布、上线、修复、迁移、安装、付款、面试或会议；
- 短期计划、临时状态和当前任务安排；
- 某个项目或任务本次完成的阶段性结果。
“完成”“放弃”“里程碑”等词本身不能把一次性事件升级为长期记忆。
如果整段内容只有 Event，三个数组都必须为空；如果同一句同时包含稳定事实和 Event，只提取其中独立的稳定事实。

每条记忆必须输出 emotional_weight（0-10），但只有候选先通过长期记忆准入规则后才评估：
- 普通事实、技术讨论、工具步骤和没有明显情绪色彩的内容 → 0
- 明确强烈喜欢/厌恶、明显受挫、关系张力或强烈在意 → 按强度给 3-9
- 不确定时保守输出 0
- 强烈情绪本身不能把临时信息升级为长期记忆
区分标准：用户是什么/拥有什么 → profile；希望怎样被服务 → preference；agent 必须怎样执行/用什么工具 → procedure。只是方向性偏好时优先 preference。

【preference / procedure 提取前四项检查，任一不通过即不提取】
检查 0 — 元讨论/举例说明
- USER 若在讨论“什么该记、怎么记、请举例”，只允许提取 USER 自己明确说出的规则。
- ASSISTANT 的例子、类比和假设场景一律不得提取。

检查 A — USER 原话锚点
- 必须能在 USER 消息中找到直接原句，不得推断。
- ASSISTANT 的解释、建议、工具结果和“USER 没反驳”都不算授权。
- “复习中”“在看书”“工作中”等纯状态汇报不提取长期规则。

检查 B — 时效性
- 本次、今天、今晚、当前项目等临时情境不提取。
- 只有明确跨 session 稳定成立才继续。

检查 C — 来源方向
- 核心内容来自 ASSISTANT 时不提取。
- ASSISTANT 主动建议后，USER 未明确说“以后都这样”或“记住这个”时不提取。

【材料层级与主体归属（适用于三类记忆）】
- USER 粘贴 transcript、聊天截图、OCR、引用文本时，外层事实只是用户正在展示材料；材料中的第一人称和 speaker 不自动等于当前 USER。
- 只有当前会话明确确认了材料 speaker 与用户或重要关系人的映射，才允许使用对应事实；映射未确认时，不得提取具体的 profile、preference 或 procedure。
- USER 在假设、举例、类比或虚构场景中使用第一人称，不是真实事实披露，不得提取。

【profile 专用规则】
- purchase：用户购买或下单了什么。
- decision：用户明确拍板的长期决定。
- status：跨 session 仍可能成立，或 USER 明确表示会持续的状态。一次性任务完成、部署完成、发布完成、故障修复完成、数据库迁移完成、单次面试和单次会议均属于 Event，不属于 profile.status；“当前”“现在”本身不足以证明是长期 status。
- personal_fact：身份、背景、持有物、爱好、习惯、经验和明确关系上下文。
- 若 existing_profile 已有相同事实，不重复输出。
- 每件具体事实单独一条；personal_fact 默认不填 happened_at。
- 工程操作、项目架构讨论、观点和具体时间事件都不是 profile。
- USER 直接陈述且具有长期价值的亲属或重要关系人事实可以提取，但 summary 必须保留“用户的姐姐/朋友”等关系主体，绝不能改写成用户本人属性。

【正反例】
<example id="keep_profile_personal_fact">
USER: 我在互联网公司做产品经理，今年30岁，住在上海，有一块 Fitbit 手表，爱好是弹钢琴。
→ profile 分别输出职业、年龄、居住地、手表和爱好五条 personal_fact，不合并。
</example>

<example id="drop_profile_memory_test">
USER: 你还记得我什么时候开始戴 fitbit 手环的吗
→ 全部为空。提问不是事实披露，绝对不反推。
</example>

<example id="profile_event_split">
USER: 这周日朋友约我去徒步，我其实不常徒步，不知道该买什么装备。
→ 可提取“用户不常徒步”；不提取“这周日去徒步”这一 event。
</example>

<example id="profile_not_preference">
USER: 我家有 10 套房，我平时爱弹钢琴，而且我有一块 Fitbit 手表
→ 三条 profile；preference/procedure 为空。
</example>

<example id="keep_relationship_subject">
USER: 我姐姐长期住在杭州
→ profile: 用户的姐姐长期住在杭州；不能改写为“用户长期住在杭州”。
</example>

<example id="drop_hypothetical_first_person">
USER: 假设我家里有三只猫，应该准备什么
→ 全部为空。“假设我家里有三只猫”不是真实事实披露。
</example>

<example id="keep_explicit_rule">
USER: 以后帮我查菜谱只给 20 分钟以内能做完的，我没时间搞复杂的
→ procedure: 查询菜谱时只推荐 20 分钟内可完成的菜式。
</example>

<example id="keep_multi_source_research">
USER: 以后帮我查耳机先看 B 站评测和 Reddit 讨论，别只看官网参数
→ procedure: 查询耳机时先看 B 站和 Reddit，不只依赖官网参数。
</example>

<example id="keep_preference_trimmed">
USER: 我不喜欢这种悬疑风格的游戏，太压抑了
ASSISTANT: 你偏好治愈系或休闲类游戏。
→ 只提取“不喜欢悬疑压抑风格”；禁止提取 ASSISTANT 延伸的治愈系偏好。
</example>

<example id="keep_preference_service_style">
USER: 你给我讲内容的时候最好附带一个很棒的例子，并且最好贯穿始终
→ preference: 讲解内容时最好附带贯穿始终的例子。
</example>

<example id="drop_situational">
USER: 今晚几个同学来，想找个气氛好的日料店
→ 全部为空；禁止推断“用户喜欢日料”。
</example>

<example id="drop_knowledge">
USER: TCP 和 UDP 的区别是什么
ASSISTANT: TCP 是可靠传输协议……
→ 全部为空；知识来自 ASSISTANT。
</example>

<example id="drop_assistant_proactive_advice">
USER: 在赶代码
ASSISTANT: 每隔一段时间起来活动下并喝水。
→ 全部为空。ASSISTANT 建议得再具体再合理，只要 USER 没有明确授权，就不是长期记忆。
</example>

<example id="drop_meta_discussion_example">
USER: 我希望只有每轮对话里真正重要的参考信息才值得存入 memory.md，你举个例子看看
ASSISTANT: 比如智能家居应坚持纯本地化部署……
→ 只允许提取 USER 自己说出的筛选标准；禁止提取智能家居示例。
</example>

<example id="drop_workaround">
USER: 那就直接写个脚本绕过去吧
→ 全部为空；这是当前任务临时策略。
</example>

【summary 写法约束】
- 只包含 USER 原话直接出现的内容，不加推断或延伸。
- 语气不得强于 USER 原话；“不太喜欢”不能升级为“强烈反感”。
- 脱离对话也能独立成立，不含“这次”“今天”“当前”等时间锚。
- 必须是完整句；profile 每条只表达一个事实。

【当前已有 profile（用于查重）】
{existing_profile or "（空）"}

【待处理对话】
{conversation}

完成判断后必须且只能调用 submit_implicit_memory；不得通过普通正文返回结果。
没有符合条件的记忆时也必须调用，并将三个参数全部设为空数组。参数结构如下：
{{
  "profile": [{{"summary": "...", "category": "personal_fact|purchase|decision|status", "happened_at": null, "emotional_weight": 0}}],
  "preference": [{{"summary": "...", "emotional_weight": 0}}],
  "procedure": [{{"summary": "...", "emotional_weight": 0, "tool_requirement": null, "steps": [], "rule_schema": {{"required_tools": [], "forbidden_tools": [], "mentioned_tools": []}}}}]
}}"""


__all__ = ["ImplicitLongTermExtractor", "ImplicitMemoryDraft"]
