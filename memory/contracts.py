"""记忆引擎与工具、Pipeline 之间共享的稳定数据契约。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol

MemoryQueryIntent = Literal["context", "answer", "timeline", "interest", "procedure"]


@dataclass(frozen=True, slots=True)
class MemoryScope:
    session_key: str = ""
    channel: str = ""
    chat_id: str = ""


@dataclass(frozen=True, slots=True)
class MemoryQueryFilters:
    kinds: tuple[str, ...] = ()
    time_start: datetime | None = None
    time_end: datetime | None = None


@dataclass(slots=True)
class MemoryQuery:
    text: str
    intent: MemoryQueryIntent = "answer"
    scope: MemoryScope = field(default_factory=MemoryScope)
    filters: MemoryQueryFilters = field(default_factory=MemoryQueryFilters)
    limit: int = 8
    context: dict[str, object] = field(default_factory=dict)
    timestamp: datetime | None = None


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    kind: str
    refs: list[str] = field(default_factory=list)
    resolver: str = "fetch_messages"
    source_ref: str = ""
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class MemoryRecord:
    id: str
    kind: str
    summary: str
    score: float = 0.0
    evidence: list[EvidenceRef] = field(default_factory=list)
    signals: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class MemoryQueryResult:
    records: list[MemoryRecord] = field(default_factory=list)
    trace: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class MemoryMutation:
    kind: Literal["remember", "forget"]
    scope: MemoryScope = field(default_factory=MemoryScope)
    summary: str = ""
    memory_kind: str = ""
    source_ref: str = ""
    ids: tuple[str, ...] = ()
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class MemoryMutationResult:
    item_id: str = ""
    status: str = "new"
    actual_kind: str = ""
    affected_ids: list[str] = field(default_factory=list)
    missing_ids: list[str] = field(default_factory=list)
    items: list[dict[str, object]] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class MemoryToolSpec:
    description: str
    parameters: dict[str, object]


@dataclass(frozen=True, slots=True)
class MemoryToolProfile:
    recall: MemoryToolSpec | None = None
    memorize: MemoryToolSpec | None = None
    forget: MemoryToolSpec | None = None


class MemoryRetrievalApi(Protocol):
    async def query(self, request: MemoryQuery) -> MemoryQueryResult: ...


class MemoryWriteApi(Protocol):
    async def mutate(self, mutation: MemoryMutation) -> MemoryMutationResult: ...


__all__ = [
    "EvidenceRef", "MemoryMutation", "MemoryMutationResult", "MemoryQuery",
    "MemoryQueryFilters", "MemoryQueryIntent", "MemoryQueryResult", "MemoryRecord",
    "MemoryRetrievalApi", "MemoryScope", "MemoryToolProfile", "MemoryToolSpec",
    "MemoryWriteApi",
]
