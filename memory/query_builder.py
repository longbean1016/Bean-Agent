"""为记忆检索构造稳定、去重的多查询集合。"""

from __future__ import annotations


def build_memory_queries(
    user_message: str,
    episodic_query: str = "",
    procedure_query: str = "",
) -> list[str]:
    """按原始消息、历史改写、流程改写的顺序返回唯一查询。

    原始消息必须保留，以免模型改写丢失专有名词或字面关键词；改写查询只作为
    辅助检索输入，不能与原文拼接成一个新的 embedding 文本。
    """

    seen: set[str] = set()
    queries: list[str] = []
    for raw in (user_message, episodic_query, procedure_query):
        value = " ".join(str(raw or "").split())
        if not value or value in seen:
            continue
        seen.add(value)
        queries.append(value)
    return queries


__all__ = ["build_memory_queries"]
