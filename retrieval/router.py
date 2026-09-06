"""The router. Given a query, return the K tools the model should see."""

from __future__ import annotations

import json
import logging
import time

from pydantic import BaseModel

import config
from retrieval.base import Hit, IndexEntry
from retrieval.index import ToolIndex

log = logging.getLogger(__name__)

# Anthropic has no local tokenizer, so the live trace shows an estimate. These two constants
# are least-squares fitted against messages.count_tokens on claude-opus-5 at 1, 5, 12, 25, 40,
# 100, 250 and 504 tools; the fit is within 2.5% across that whole range. A flat
# characters-per-token guess is not good enough - serialising the tool block costs a fixed
# ~287 tokens before the first schema, which at k=12 is 17% of the total.
CHARS_PER_TOKEN = 2.36
TOOL_BLOCK_OVERHEAD_TOKENS = 287


class Routing(BaseModel):
    query: str
    k: int
    tools: list[IndexEntry]
    hits: list[Hit]
    widened: bool = False
    duration_ms: float = 0.0
    estimated_tokens_selected: int = 0
    estimated_tokens_all: int = 0

    @property
    def estimated_tokens_saved(self) -> int:
        return max(0, self.estimated_tokens_all - self.estimated_tokens_selected)

    # This property summarises which servers the selection came from, for the trace.
    def servers(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for tool in self.tools:
            counts[tool.server] = counts.get(tool.server, 0) + 1
        return counts


def schema_text(entry: IndexEntry) -> str:
    # This function approximates what one tool costs in the prompt.
    return json.dumps({"name": entry.name, "description": entry.description,
                       "input_schema": {"type": "object",
                                        "properties": {p: {"type": "string"}
                                                       for p in entry.param_names}}})


def estimate_tokens(entries: list[IndexEntry]) -> int:
    # This function estimates the prompt cost of a set of tool schemas, overhead included.
    if not entries:
        return 0
    chars = sum(len(schema_text(e)) for e in entries)
    return int(TOOL_BLOCK_OVERHEAD_TOKENS + chars / CHARS_PER_TOKEN)


def route(index: ToolIndex, query: str, k: int | None = None,
          retriever: str | None = None, widened: bool = False) -> Routing:
    # This function returns the tools the model should see for this query.
    k = config.ROUTER_K if k is None else k
    retriever = config.ROUTER_RETRIEVER if retriever is None else retriever
    started = time.perf_counter()
    hits = index.search(query, k, retriever)
    tools = index.resolve(hits)
    elapsed = (time.perf_counter() - started) * 1000

    routing = Routing(
        query=query, k=k, tools=tools, hits=hits, widened=widened, duration_ms=elapsed,
        estimated_tokens_selected=estimate_tokens(tools),
        estimated_tokens_all=estimate_tokens(index.entries),
    )
    log.info("routed %d tools from %s in %.1fms", len(tools), routing.servers(), elapsed)
    return routing


def widen(index: ToolIndex, query: str, k: int | None = None,
          retriever: str = "hybrid") -> Routing:
    # This function re-runs the route at a larger k after the model said "wrong toolbox".
    # Re-ranked, not appended: a larger cut can surface tools the smaller one missed entirely.
    return route(index, query, config.WIDEN_K if k is None else k, retriever, widened=True)
