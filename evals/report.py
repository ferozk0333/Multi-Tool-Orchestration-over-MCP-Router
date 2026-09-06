"""Scores the router against the goldens: recall@K per retriever, split by query type."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from catalog.models import load_catalog  # noqa: E402
from retrieval.index import ToolIndex, entries_from_catalog  # noqa: E402

RETRIEVERS = ("bm25", "dense", "hybrid")
Split = Literal["lexical", "paraphrase"]


class Golden(BaseModel):
    query: str
    expected_key: str        # "service/tool_name", matching IndexEntry.key
    service: str
    source: str              # real | synthetic | real_additive
    split: Split
    checked: bool = False    # reviewed by hand
    flag: str | None = None  # why this golden is unsound, if it is


def load_goldens(path: Path | None = None) -> list[Golden]:
    # This function loads the labelled query/tool pairs.
    raw = json.loads((path or config.GOLDENS_PATH).read_text())
    return [Golden.model_validate(g) for g in raw["goldens"]]


def rank_of(index: ToolIndex, golden: Golden, retriever: str, k: int) -> int | None:
    # This function returns the 1-based rank of the expected tool, or None if it missed.
    hits = index.search(golden.query, k, retriever)
    return next((h.rank for h in hits if h.tool_key == golden.expected_key), None)


def recall_at_k(index: ToolIndex, goldens: list[Golden], retriever: str, k: int) -> float:
    # This function returns the fraction of goldens whose expected tool made the top k.
    if not goldens:
        return 0.0
    found = sum(rank_of(index, g, retriever, k) is not None for g in goldens)
    return found / len(goldens)


def _row(label: str, values: list[str]) -> str:
    return f"| {label:<22} | " + " | ".join(f"{v:>9}" for v in values) + " |"


def main() -> int:
    goldens = load_goldens()
    index = ToolIndex(entries_from_catalog(load_catalog()))
    k = config.ROUTER_K

    keys = {e.key for e in index.entries}
    missing = [g.expected_key for g in goldens if g.expected_key not in keys]
    if missing:
        raise SystemExit(f"goldens reference tools not in the catalogue: {missing[:5]}")

    clean = [g for g in goldens if g.flag is None]
    subsets: list[tuple[str, list[Golden]]] = [
        ("all", goldens),
        ("lexical", [g for g in goldens if g.split == "lexical"]),
        ("paraphrase", [g for g in goldens if g.split == "paraphrase"]),
        ("real tools", [g for g in goldens if g.source != "synthetic"]),
        ("synthetic tools", [g for g in goldens if g.source == "synthetic"]),
        ("unflagged", clean),
        ("unflagged paraphrase", [g for g in clean if g.split == "paraphrase"]),
    ]

    print(f"\nrecall@{k} over {len(goldens)} goldens, {len(index)} tools indexed\n")
    print(_row("subset (n)", [r for r in RETRIEVERS]))
    print("|" + "-" * 24 + "|" + ("|".join(["-" * 11] * len(RETRIEVERS))) + "|")
    for label, subset in subsets:
        values = [f"{recall_at_k(index, subset, r, k):.1%}" for r in RETRIEVERS]
        print(_row(f"{label} ({len(subset)})", values))

    checked = sum(g.checked for g in goldens)
    flagged = [g for g in goldens if g.flag]
    print(f"\nhand-checked: {checked}/{len(goldens)} ({checked / len(goldens):.0%}) "
          f"· flagged: {len(flagged)}")
    for g in flagged:
        print(f"  {g.expected_key}: {g.flag}")
    print("\nrecall@K is a lower bound: expected_key is the tool each query was generated")
    print("from, and other tools in the catalogue may also answer it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
