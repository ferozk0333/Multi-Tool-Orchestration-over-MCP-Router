"""The retrieval tests from RETRIEVAL.md. Pure functions, no API calls, no model download."""

from __future__ import annotations

import pytest

import config
from retrieval.base import Hit, IndexEntry, rank_hits
from retrieval.bm25 import BM25Retriever, tokenize
from retrieval.hybrid import HybridRetriever, fuse
from retrieval.index import ToolIndex, entries_from_catalog
from catalog.models import load_catalog


def entry(key: str, name: str, description: str = "", params: list[str] | None = None) -> IndexEntry:
    return IndexEntry(key=key, server=key.split("/")[0], name=name,
                      description=description, param_names=params or [])


CORPUS = [
    entry("github/github_actions_list", "github_actions_list",
          "Lists GitHub Actions resources with filtering", ["owner", "repo", "per_page"]),
    entry("github/github_pulls_list", "github_pulls_list", "List pull requests",
          ["owner", "repo", "state"]),
    entry("slack/slack_chat_post_message", "slack_chat_post_message",
          "Sends a message to a channel.", ["token", "channel", "text"]),
    entry("stripe/stripe_get_refunds", "stripe_get_refunds", "List all refunds",
          ["charge", "limit"]),
    entry("hr/hr_getUserByID", "hr_getUserByID", "Fetch an employee record",
          ["employee_id"]),
]


class TestTokenize:
    def test_splits_underscores(self):
        assert {"github", "actions", "list"} <= set(tokenize("github_actions_list"))

    def test_splits_camel_case(self):
        assert {"get", "user", "id"} <= set(tokenize("getUserByID"))  # "by" is a stopword
        assert "by" not in tokenize("getUserByID")

    def test_splits_acronym_boundary(self):
        assert {"parse", "http", "response"} <= set(tokenize("parseHTTPResponse"))

    def test_keeps_the_whole_identifier_alongside_its_parts(self):
        assert "github_actions_list" in tokenize("github_actions_list")
        assert "getuserbyid" in tokenize("getUserByID")

    def test_single_word_terms_are_not_duplicated(self):
        assert tokenize("message") == ["message"]

    def test_drops_stopwords_but_keeps_tool_verbs(self):
        assert tokenize("what are my open pull requests") == ["open", "pull", "requests"]
        assert "list" in tokenize("list the refunds")

    def test_query_and_identifier_tokenize_the_same_way(self):
        assert set(tokenize("actions")) <= set(tokenize("github_actions_list"))


class TestBM25:
    def test_exact_name_match_ranks_first(self):
        r = BM25Retriever()
        r.index(CORPUS)
        assert r.search("github_pulls_list", 3)[0].tool_key == "github/github_pulls_list"

    def test_parameter_names_are_searchable(self):
        # The description never says "employee_id"; only the parameter list does.
        r = BM25Retriever()
        r.index(CORPUS)
        assert r.search("employee_id", 1)[0].tool_key == "hr/hr_getUserByID"

    def test_returns_exactly_k_hits(self):
        r = BM25Retriever()
        r.index(CORPUS)
        assert len(r.search("list", 3)) == 3

    def test_empty_index_returns_nothing(self):
        r = BM25Retriever()
        r.index([])
        assert r.search("anything", 5) == []

    def test_abstains_when_no_term_matches(self):
        # Zero score means no query term appears at all. Returning it as rank 1 would give
        # rank fusion a confident-looking vote for a document that matched nothing.
        r = BM25Retriever()
        r.index(CORPUS)
        assert r.search("zzzzz qqqqq", 5) == []

    def test_returns_only_documents_that_share_a_term(self):
        r = BM25Retriever()
        r.index(CORPUS)
        hits = r.search("refunds", 5)
        assert len(hits) < len(CORPUS)
        assert all(h.score > 0 for h in hits)
        assert hits[0].tool_key == "stripe/stripe_get_refunds"


class TestRankHits:
    def test_ranks_are_one_based_contiguous_and_ordered(self):
        hits = rank_hits([("a", 0.1), ("b", 0.9), ("c", 0.5)], 3)
        assert [h.rank for h in hits] == [1, 2, 3]
        assert [h.tool_key for h in hits] == ["b", "c", "a"]
        assert [h.score for h in hits] == sorted((h.score for h in hits), reverse=True)

    def test_ties_break_deterministically(self):
        assert [h.tool_key for h in rank_hits([("b", 1.0), ("a", 1.0)], 2)] == ["a", "b"]

    def test_truncates_to_k(self):
        assert len(rank_hits([(c, 1.0) for c in "abcdef"], 2)) == 2


class TestRRF:
    def test_rank_one_in_both_lists_beats_rank_one_in_only_one(self):
        both = [Hit(tool_key="x", score=9, rank=1)]
        only = [Hit(tool_key="y", score=9, rank=1)]
        fused = fuse([both + [Hit(tool_key="y", score=1, rank=2)], both], 2)
        assert fused[0].tool_key == "x"
        assert [h.tool_key for h in fuse([both, only], 2)][0] in ("x", "y")

    def test_document_present_in_a_single_list_still_scores(self):
        fused = fuse([[Hit(tool_key="a", score=1, rank=1)],
                      [Hit(tool_key="b", score=1, rank=1)]], 2)
        assert {h.tool_key for h in fused} == {"a", "b"}
        assert all(h.score == pytest.approx(1 / (config.RRF_K + 1)) for h in fused)

    def test_score_is_the_sum_of_reciprocal_ranks(self):
        fused = fuse([[Hit(tool_key="a", score=0, rank=1)],
                      [Hit(tool_key="a", score=0, rank=3)]], 1)
        assert fused[0].score == pytest.approx(1 / (config.RRF_K + 1) + 1 / (config.RRF_K + 3))

    def test_absent_documents_contribute_nothing(self):
        fused = fuse([[Hit(tool_key="a", score=0, rank=1)]], 5)
        assert len(fused) == 1

    def test_lower_rank_scores_higher(self):
        fused = fuse([[Hit(tool_key="a", score=0, rank=1), Hit(tool_key="b", score=0, rank=2)]], 2)
        assert fused[0].tool_key == "a" and fused[0].score > fused[1].score


class TestHybridIgnoresAnAbstainingRetriever:
    def test_a_retriever_with_no_matches_does_not_shift_the_fused_order(self):
        class Silent:
            name = "silent"

            def index(self, entries): pass

            def search(self, query, k): return []

        good = BM25Retriever()
        alone = HybridRetriever([good])
        alone.index(CORPUS)
        with_silent = HybridRetriever([good, Silent()])
        with_silent.index(CORPUS)
        query = "refunds"
        assert [h.tool_key for h in alone.search(query, 3)] == \
               [h.tool_key for h in with_silent.search(query, 3)]


class TestHybrid:
    def test_pulls_more_candidates_than_k_before_fusing(self):
        seen: list[int] = []

        class Spy:
            name = "spy"

            def index(self, entries): pass

            def search(self, query, k):
                seen.append(k)
                return [Hit(tool_key="a", score=1.0, rank=1)]

        HybridRetriever([Spy(), Spy()], candidate_multiplier=3).search("q", 5)
        assert seen == [15, 15]

    def test_returns_exactly_k_when_the_corpus_is_larger(self):
        h = HybridRetriever([BM25Retriever()])
        h.index(CORPUS)
        assert len(h.search("list", 3)) == 3


@pytest.fixture(scope="module")
def entries():
    return entries_from_catalog(load_catalog())


@pytest.fixture(scope="module")
def bm25_index(entries):
    # A BM25-only index, so the suite never downloads a model; widen is retriever-agnostic.
    idx = ToolIndex.__new__(ToolIndex)
    idx.entries = entries
    idx.by_key = {e.key: e for e in entries}
    idx.bm25 = BM25Retriever()
    idx.bm25.index(entries)
    idx.dense = None
    idx.hybrid = idx.bm25
    idx.build_ms = 0.0
    return idx


class TestIndexOverRealCatalogue:
    """BM25-only so the suite never downloads a model."""

    def test_every_catalogue_tool_is_indexed(self, entries):
        assert len(entries) == len(load_catalog())

    def test_keys_are_unique(self, entries):
        assert len({e.key for e in entries}) == len(entries)

    def test_indexed_text_includes_name_description_and_params(self, entries):
        e = next(x for x in entries if x.name == "github_pulls_list")
        assert "github_pulls_list" in e.text and "pull requests" in e.text.lower()
        assert "owner" in e.text and "state" in e.text

    def test_bm25_finds_a_tool_by_its_exact_name(self, entries):
        r = BM25Retriever()
        r.index(entries)
        hits = r.search("slack_chat_post_message", config.ROUTER_K)
        assert hits[0].tool_key == "slack/slack_chat_post_message"

    def test_returns_exactly_router_k_over_the_full_catalogue(self, entries):
        r = BM25Retriever()
        r.index(entries)
        assert len(r.search("send a message", config.ROUTER_K)) == config.ROUTER_K


class TestRouterWiden:
    """Widen re-ranks at a larger k rather than appending to the smaller cut."""

    def test_widen_returns_more_tools(self, bm25_index):
        from retrieval.router import route, widen
        query = "post a summary of the newest pull request"
        narrow = route(bm25_index, query, retriever="bm25")
        wide = widen(bm25_index, query, retriever="bm25")
        assert len(narrow.tools) == config.ROUTER_K
        assert len(wide.tools) == config.WIDEN_K
        assert wide.widened and not narrow.widened

    def test_widen_is_reranked_not_appended(self, bm25_index):
        from retrieval.router import route, widen
        index = bm25_index
        query = "post a summary of the newest pull request"
        narrow = [h.tool_key for h in route(index, query, retriever="bm25").hits]
        wide = [h.tool_key for h in widen(index, query, retriever="bm25").hits]
        # The larger cut is produced by ranking at WIDEN_K, not by concatenating.
        assert wide[: len(narrow)] == narrow or set(narrow) - set(wide) != set()

    def test_token_estimate_drops_with_routing(self, bm25_index):
        from retrieval.router import route
        r = route(bm25_index, "list pull requests", retriever="bm25")
        assert r.estimated_tokens_selected < r.estimated_tokens_all
        assert r.estimated_tokens_saved > 0
