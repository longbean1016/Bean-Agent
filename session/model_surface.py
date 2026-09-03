"""模型侧 surface 使用的稳定协议文本。"""

TOOL_NOT_STARTED_RESULT_CONTENT = (
    "工具调用已由模型生成，但中断发生在工具真正启动前；没有执行结果。"
    "请根据工具语义决定是否重新调用：只读或幂等操作可以重试；"
    "可能产生副作用时先询问用户或核验外部状态。"
)

TOOL_OUTCOME_UNKNOWN_RESULT_CONTENT = (
    "工具调用在中断前已经发出，但没有记录完整结果；结果未知。"
    "请根据工具语义决定是否重试：只有只读或幂等操作可以重试；"
    "可能产生副作用时先核验外部状态或询问用户，不要盲目重试。"
)

# 兼容旧的调用方；新 repair 必须按事件日志选择更精确的常量。
INTERRUPTED_TOOL_RESULT_CONTENT = TOOL_OUTCOME_UNKNOWN_RESULT_CONTENT

__all__ = [
    "INTERRUPTED_TOOL_RESULT_CONTENT",
    "TOOL_NOT_STARTED_RESULT_CONTENT",
    "TOOL_OUTCOME_UNKNOWN_RESULT_CONTENT",
]
