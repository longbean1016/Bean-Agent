"""模型侧 surface 使用的稳定协议文本。"""

INTERRUPTED_TOOL_RESULT_CONTENT = (
    "工具调用在中断前已经发出，但没有记录完整结果；结果未知。"
    "请根据工具语义决定是否重试：只有只读或幂等操作可以重试；"
    "可能产生副作用时先核验外部状态或询问用户，不要盲目重试。"
)

__all__ = ["INTERRUPTED_TOOL_RESULT_CONTENT"]
