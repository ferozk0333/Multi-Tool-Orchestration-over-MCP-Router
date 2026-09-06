"""The one interface the three retrievers share, plus the entry they rank over."""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel


class IndexEntry(BaseModel):
    key: str            # pool-wide unique, e.g. "github/github_pulls_list"
    server: str
    name: str
    description: str
    # The tool's JSON Schema properties, types included. Types are carried, not discarded:
    # declaring every parameter as a string makes the model send "100" for an integer field
    # and the server rejects it.
    properties: dict[str, Any] = {}
    required: list[str] = []

    @property
    def param_names(self) -> list[str]:
        return list(self.properties)

    # This property is the text every retriever indexes. Parameter names are included
    # deliberately: descriptions here are terse, so params are often the only place a
    # query's words appear at all.
    @property
    def text(self) -> str:
        return f"{self.name} {self.description} {' '.join(self.param_names)}"


class Hit(BaseModel):
    tool_key: str
    score: float
    rank: int           # 1-based, contiguous, ordered by score


class Retriever(Protocol):
    name: str

    def index(self, entries: list[IndexEntry]) -> None: ...

    def search(self, query: str, k: int) -> list[Hit]: ...


def rank_hits(scored: list[tuple[str, float]], k: int) -> list[Hit]:
    # This function turns (key, score) pairs into the top k hits with 1-based contiguous ranks.
    # Ties break on key so a run is reproducible rather than dependent on sort stability.
    ordered = sorted(scored, key=lambda kv: (-kv[1], kv[0]))[:k]
    return [Hit(tool_key=key, score=score, rank=i) for i, (key, score) in enumerate(ordered, 1)]
