"""BM25 over the tool text, with tokenisation that splits identifiers apart."""

from __future__ import annotations

import re

from rank_bm25 import BM25Okapi

import config
from retrieval.base import Hit, IndexEntry, rank_hits

# Small and deliberate. Tool words like "list", "get" and "create" are meaningful here,
# so they stay in.
STOPWORDS = {
    "a", "an", "and", "any", "are", "as", "at", "be", "by", "can", "do", "does", "for",
    "from", "has", "have", "how", "i", "if", "in", "is", "it", "its", "me", "my", "of",
    "on", "or", "please", "that", "the", "their", "them", "there", "this", "to", "was",
    "we", "what", "when", "which", "who", "will", "with", "you", "your",
}

_LOWER_UPPER = re.compile(r"([a-z0-9])([A-Z])")
_ACRONYM = re.compile(r"([A-Z]+)([A-Z][a-z])")
_NON_ALNUM = re.compile(r"[^A-Za-z0-9]+")
_TERMS = re.compile(r"[A-Za-z0-9_]+")


def _split_identifier(term: str) -> list[str]:
    # This function breaks one identifier on underscores and camelCase boundaries.
    spaced = _ACRONYM.sub(r"\1 \2", _LOWER_UPPER.sub(r"\1 \2", term))
    return _NON_ALNUM.sub(" ", spaced).lower().split()


def tokenize(text: str) -> list[str]:
    # This function emits an identifier's parts and, when it has more than one, the whole
    # identifier too.
    #
    # The split alone is not enough: github_actions_list has to match a query about
    # "actions", but a query naming a tool exactly has to find that tool, and BM25's length
    # normalisation otherwise lets a short neighbouring tool outrank the exact match just
    # because the right tool has a long parameter list. The unsplit identifier is a rare,
    # highly discriminative term, so keeping it settles that case.
    tokens: list[str] = []
    for term in _TERMS.findall(text):
        parts = _split_identifier(term)
        tokens.extend(p for p in parts if p not in STOPWORDS)
        if len(parts) > 1:
            tokens.append(term.lower())
    return tokens


class BM25Retriever:
    name = "bm25"

    def __init__(self) -> None:
        self.entries: list[IndexEntry] = []
        self._bm25: BM25Okapi | None = None

    def index(self, entries: list[IndexEntry]) -> None:
        # This method builds the BM25 index over the entries' indexed text.
        self.entries = entries
        corpus = [tokenize(e.text) for e in entries]
        self._bm25 = BM25Okapi(corpus, k1=config.BM25_K1, b=config.BM25_B) if corpus else None

    def search(self, query: str, k: int) -> list[Hit]:
        # This method returns up to k lexical matches, dropping documents that share no term
        # with the query.
        #
        # A BM25 score of zero means no query term appears in the document at all. That is the
        # absence of a hit, not a weak one, and returning it as rank 1 poisons rank fusion:
        # RRF sees ranks, not scores, so a zero-score rank 1 counts exactly as much as a
        # genuine one. "builds failing in CI" matches nothing lexically in this catalogue, and
        # without this filter its arbitrary zero-score ordering outvoted the dense retriever.
        if self._bm25 is None:
            return []
        scores = self._bm25.get_scores(tokenize(query))
        scored = [(e.key, float(s)) for e, s in zip(self.entries, scores) if s > 0]
        return rank_hits(scored, k)
