"""Generates the labelled query/tool pairs. Claude writes the queries, the sampling is seeded."""

from __future__ import annotations

import json
import os
import random
import re
import sys
from pathlib import Path

import anthropic

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from catalog.models import Tool, load_catalog  # noqa: E402
from evals.report import Golden  # noqa: E402

MODEL = "claude-opus-5"

# service -> (lexical, paraphrase). 60 total, weighted at the demo, 30/30 by split.
PLAN: dict[str, tuple[int, int]] = {
    "github": (10, 10),
    "slack": (8, 8),
    "stripe": (2, 2),
    "twilio": (1, 2),
    "crm": (2, 1),
    "ticketing": (1, 2),
    "calendar": (2, 1),
    "storage": (1, 1),
    "analytics": (1, 1),
    "hr": (1, 1),
    "inventory": (1, 1),
}

INSTRUCTIONS = {
    "lexical": (
        "The user knows this system and refers to the operation or its fields by name. "
        "Reuse the tool's own vocabulary, including the identifier or parameter names where "
        "it reads naturally."
    ),
    "paraphrase": (
        "The user describes what they want in their own words and does NOT know the API. "
        "Do not use the tool's name, and avoid the distinctive content words from its "
        "description and parameter names. Describe the intent, not the endpoint."
    ),
}

PROMPT = """You are writing evaluation queries for a tool-retrieval system.

Below are {n} tools from a {service} API. For each one, write a single realistic user request
that this tool, and not a generic sibling, would answer.

{instruction}

Rules for every query:
- One sentence, under 15 words, phrased as a person talking to an assistant.
- Specific enough that this tool is a defensible answer.
- No tool numbers, no JSON, no quotes around the query.

Tools:
{tools}

Return ONLY a JSON array, no prose or code fences:
[{{"index": 1, "query": "..."}}, ...]
"""


def build_client() -> anthropic.Anthropic:
    # This function builds the client, adding the workspace header an unscoped key requires.
    workspace = os.environ.get("ANTHROPIC_WORKSPACE_ID")
    headers = {"anthropic-workspace-id": workspace} if workspace else None
    return anthropic.Anthropic(default_headers=headers)


def sample_tools(catalog: list[Tool]) -> list[tuple[Tool, str]]:
    # This function picks the tools to generate from, seeded so a re-run is reproducible.
    rng = random.Random(config.SEED)
    picked: list[tuple[Tool, str]] = []
    for service, (n_lex, n_par) in PLAN.items():
        pool = [t for t in catalog if t.service == service]
        chosen = rng.sample(pool, n_lex + n_par)
        picked.extend((t, "lexical") for t in chosen[:n_lex])
        picked.extend((t, "paraphrase") for t in chosen[n_lex:])
    return picked


def describe(tool: Tool, n: int) -> str:
    # This function renders one tool for the prompt.
    params = ", ".join(p.name for p in tool.parameters[:8]) or "none"
    return f"{n}. {tool.name}\n   description: {tool.description}\n   parameters: {params}"


def parse_queries(text: str, expected: int) -> dict[int, str]:
    # This function pulls the JSON array out of the reply, tolerating a stray code fence.
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if match is None:
        raise ValueError(f"no JSON array in reply: {text[:200]}")
    rows = json.loads(match.group(0))
    out = {int(r["index"]): r["query"].strip() for r in rows}
    if len(out) != expected:
        raise ValueError(f"expected {expected} queries, got {len(out)}")
    return out


def generate_group(client: anthropic.Anthropic, service: str, split: str,
                   tools: list[Tool]) -> list[str]:
    # This function asks Claude for one query per tool in a single call.
    prompt = PROMPT.format(
        n=len(tools), service=service, instruction=INSTRUCTIONS[split],
        tools="\n".join(describe(t, i) for i, t in enumerate(tools, 1)),
    )
    response = client.messages.create(
        model=MODEL, max_tokens=16000,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(b.text for b in response.content if b.type == "text")
    queries = parse_queries(text, len(tools))
    return [queries[i] for i in range(1, len(tools) + 1)]


def leaks_tool_name(query: str, tool: Tool) -> bool:
    # This function flags a paraphrase query that used the identifier it was told to avoid.
    words = set(re.split(r"[^a-z0-9]+", query.lower()))
    stem = tool.name.split("_", 1)[1] if "_" in tool.name else tool.name
    return tool.name.lower() in query.lower() or all(
        part in words for part in stem.split("_") if len(part) > 3
    )


def main() -> int:
    catalog = list(load_catalog())
    picked = sample_tools(catalog)

    groups: dict[tuple[str, str], list[Tool]] = {}
    for tool, split in picked:
        groups.setdefault((tool.service, split), []).append(tool)

    client = build_client()
    goldens: list[Golden] = []
    leaks: list[str] = []

    for (service, split), tools in groups.items():
        print(f"  {service:<12} {split:<11} {len(tools)} queries", file=sys.stderr)
        for tool, query in zip(tools, generate_group(client, service, split, tools)):
            if split == "paraphrase" and leaks_tool_name(query, tool):
                leaks.append(f"{tool.name}: {query}")
            goldens.append(Golden(query=query, expected_key=f"{tool.service}/{tool.name}",
                                  service=tool.service, source=tool.source, split=split))

    config.GOLDENS_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.GOLDENS_PATH.write_text(
        json.dumps({"goldens": [g.model_dump() for g in goldens]}, indent=2) + "\n"
    )

    print(f"\n{len(goldens)} goldens -> {config.GOLDENS_PATH}")
    print(f"split: {sum(g.split == 'lexical' for g in goldens)} lexical, "
          f"{sum(g.split == 'paraphrase' for g in goldens)} paraphrase")
    if leaks:
        print(f"\n{len(leaks)} paraphrase queries leaked the tool name - review these first:")
        for line in leaks:
            print(f"  {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
