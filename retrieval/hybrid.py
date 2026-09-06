"""Reciprocal rank fusion over the base retrievers. Ranks only, so nothing needs calibrating."""

from __future__ import annotations

from collections import defaultdict

import config
from retrieval.base import Hit, IndexEntry, Retriever, rank_hits


def fuse(rankings: list[list[Hit]], k: int, rrf_k: int | None = None) -> list[Hit]:
    # This function fuses several ranked lists: score(d) = sum over lists of 1 / (RRF_K + rank).
    # BM25 scores are unbounded and cosine sits in [-1, 1]; blending those needs tuning that
    # would not survive a change of catalogue. Ranks need none.
    constant = config.RRF_K if rrf_k is None else rrf_k
    scores: dict[str, float] = defaultdict(float)
    for hits in rankings:
        for hit in hits:
            scores[hit.tool_key] += 1.0 / (constant + hit.rank)
    return rank_hits(list(scores.items()), k)


class HybridRetriever:
    name = "hybrid"

    def __init__(self, retrievers: list[Retriever],
                 candidate_multiplier: int | None = None) -> None:
        self.retrievers = retrievers
        self.candidate_multiplier = (
            config.HYBRID_CANDIDATE_MULTIPLIER if candidate_multiplier is None
            else candidate_multiplier
        )

    def index(self, entries: list[IndexEntry]) -> None:
        # This method indexes every base retriever over the same entries.
        for retriever in self.retrievers:
            retriever.index(entries)

    def search(self, query: str, k: int) -> list[Hit]:
        # This method fuses the base rankings, pulling extra candidates from each first so a
        # tool just outside one list's cut can still be rescued by the other.
        depth = k * self.candidate_multiplier
        return fuse([r.search(query, depth) for r in self.retrievers], k)
