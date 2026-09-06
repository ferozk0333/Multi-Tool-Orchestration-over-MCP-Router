"""Builds the searchable index from either the catalogue or the pool's aggregated tools."""

from __future__ import annotations

import logging
import time
from typing import Any, Iterable

import config
from catalog.models import Tool
from retrieval.base import Hit, IndexEntry, Retriever
from retrieval.bm25 import BM25Retriever
from retrieval.dense import DenseRetriever
from retrieval.hybrid import HybridRetriever

log = logging.getLogger(__name__)


def entries_from_catalog(tools: Iterable[Tool]) -> list[IndexEntry]:
    # This function builds index entries from the committed catalogue, for offline evals.
    #
    # Credential parameters are dropped here exactly as the servers drop them from the schemas
    # they advertise. Otherwise the eval would index text the live system never sees, and
    # recall@K would be measured against a catalogue that does not exist at runtime.
    return [
        IndexEntry(key=f"{t.service}/{t.name}", server=t.service, name=t.name,
                   description=t.description,
                   properties={p.name: {"type": p.type} for p in t.parameters
                               if p.name not in config.CREDENTIAL_PARAMS},
                   required=[p.name for p in t.parameters
                             if p.required and p.name not in config.CREDENTIAL_PARAMS])
        for t in tools
    ]


def entries_from_pool(tools: Iterable[Any]) -> list[IndexEntry]:
    # This function builds index entries from live tools/list results, so a downed server's
    # tools are genuinely absent from the index rather than filtered out later.
    return [
        IndexEntry(key=t.qualified_name, server=t.server, name=t.name,
                   description=t.description,
                   properties=(t.input_schema or {}).get("properties", {}),
                   required=(t.input_schema or {}).get("required", []))
        for t in tools
    ]


class ToolIndex:
    """One index, three retrievers over identical text."""

    def __init__(self, entries: list[IndexEntry], use_cache: bool = True,
                 warm: bool = True) -> None:
        self.entries = entries
        self.by_key = {e.key: e for e in entries}
        self.bm25 = BM25Retriever()
        self.dense = DenseRetriever(use_cache=use_cache)
        self.hybrid = HybridRetriever([self.bm25, self.dense])

        started = time.perf_counter()
        self.hybrid.index(entries)
        if warm:
            self.dense.warm()
        self.build_ms = (time.perf_counter() - started) * 1000
        log.info("indexed %d tools in %.0fms", len(entries), self.build_ms)

    def retriever(self, name: str) -> Retriever:
        # This method resolves a retriever by name, for the eval that compares all three.
        return {"bm25": self.bm25, "dense": self.dense, "hybrid": self.hybrid}[name]

    def search(self, query: str, k: int, retriever: str = "hybrid") -> list[Hit]:
        # This method returns the k best tools for the query.
        return self.retriever(retriever).search(query, k)

    def resolve(self, hits: list[Hit]) -> list[IndexEntry]:
        # This method turns hits back into the entries they point at.
        return [self.by_key[h.tool_key] for h in hits]

    def __len__(self) -> int:
        return len(self.entries)
