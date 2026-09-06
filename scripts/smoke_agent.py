"""Step 4 proof: the four scenarios, plus the branches that only fire if made to fire."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from agent.loop import Agent  # noqa: E402
from catalog.models import load_catalog  # noqa: E402
from mcp_client.pool import ServerPool  # noqa: E402
from retrieval.index import ToolIndex, entries_from_pool  # noqa: E402

failures: list[str] = []
runs: list[tuple[str, list]] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not condition:
        failures.append(label)


async def collect(agent: Agent, query: str, show: bool = True,
                  name: str | None = None) -> list[Any]:
    # This function runs one query and renders the trace as it streams.
    events = []
    async for ev in agent.run(query):
        events.append(ev)
        if show:
            print(f"      {render(ev)}")
    if name:
        runs.append((name, events))
    return events


def render(ev: Any) -> str:
    # This function renders one event the way the UI will.
    d = ev.data
    if ev.type == "tools_selected":
        return (f"selected {d['k']} tools  {d['servers']}  "
                f"~{d['estimated_tokens_saved']:,} tokens saved")
    if ev.type == "tool_call_started":
        return f"{d['tool']} {json.dumps(d['arguments'])[:80]}"
    if ev.type == "tool_call_finished":
        mark = "ok" if d["ok"] else "FAILED"
        return (f"  {mark} {d['duration_ms']:.0f}ms attempt(s)={d['attempts']} "
                f"{d['summary'] or d['error']}"[:150])
    if ev.type == "tools_widened":
        return f"WIDENED {d.get('old_k')} -> {d.get('new_k')}  reason: {d.get('reason', '')[:60]}"
    if ev.type == "final_answer":
        return f"ANSWER ({d['turns']} turns, {d['duration_ms']:.0f}ms): {d['text'][:200]}"
    if ev.type == "clarification_requested":
        return f"CLARIFY: {d['question'][:120]}"
    if ev.type == "error":
        return f"ERROR {d}"
    return ev.type


def of(events: list[Any], type_: str) -> list[Any]:
    return [e for e in events if e.type == type_]


async def main() -> int:
    async with ServerPool() as pool:
        index = ToolIndex(entries_from_pool(pool.tools()))
        agent = Agent(pool, index)
        print(f"\n{len(pool.tools())} tools aggregated, indexed {len(index)}\n")

        print("SCENARIO 1 — single tool")
        ev = await collect(agent, "what are my open pull requests on acme/api-server?", name="1 single tool")
        calls = of(ev, "tool_call_finished")
        check("at least one tool call", len(calls) >= 1, f"{len(calls)} calls")
        check("every call succeeded", all(c.data["ok"] for c in calls),
              str([c.data["error"] for c in calls if not c.data["ok"]]))
        check("github was used", any("github" in c.data["tool"] for c in calls))
        answer = of(ev, "final_answer")[0].data["text"]
        check("the answer names PR #482", "482" in answer)
        check("routing saved tokens",
              of(ev, "tools_selected")[0].data["estimated_tokens_saved"] > 40000)

        print("\nSCENARIO 2 — cross-server chain")
        ev = await collect(agent, "Find the newest open PR on acme/api-server and post a "
                                  "one-line summary of it to the eng channel in Slack.", name="2 cross-server")
        calls = of(ev, "tool_call_finished")
        servers = {c.data["tool"].split("/")[0] for c in calls if c.data["ok"]}
        check("both servers were called", {"github", "slack"} <= servers, str(servers))
        check("every call succeeded", all(c.data["ok"] for c in calls),
              str([c.data["error"] for c in calls if not c.data["ok"]]))

        posted = await pool.call("slack/slack_conversations_history",
                                 {"token": "x", "channel": "eng", "limit": 3})
        texts = [m["text"] for m in posted.payload.get("messages", [])]
        check("Slack world state actually changed", any("482" in t for t in texts),
              json.dumps(texts[:2]))

        print("\nSCENARIO 3 — ambiguous, expect a question and no tool calls")
        ev = await collect(agent, "send them the file", name="3 ambiguous")
        calls = of(ev, "tool_call_started")
        check("no tools were called", len(calls) == 0, f"{len(calls)} calls")
        check("a clarifying question was asked", len(of(ev, "clarification_requested")) == 1)

        print("\nSCENARIO 4 — retrieval misses, expect widen and recovery")
        # Retrieval on the model's stated need is accurate enough that the wrong toolbox no
        # longer happens by accident, so force it: at k=3 the first cut cannot hold the tools
        # for a two-service task, and the model has to ask again. recall@12 is 91.7%, so the
        # real system lands in this branch on the ~8% tail rather than never.
        narrow = config.ROUTER_K
        config.ROUTER_K = 3
        try:
            ev = await collect(agent, "Find the newest open PR on acme/api-server and post a "
                                      "one-line summary of it to the eng channel in Slack.",
                               name="4 widen on a miss")
        finally:
            config.ROUTER_K = narrow

        widened = of(ev, "tools_widened")
        check("the model asked again when the tools fell short", len(widened) >= 1,
              widened[0].data.get("reason", "")[:80] if widened else "never fired")
        check("the tool set grew", widened and widened[0].data["new_k"] > 3,
              f"{widened[0].data['old_k']} -> {widened[0].data['new_k']}" if widened else "")
        calls = of(ev, "tool_call_finished")
        check("it recovered with a real call", any(c.data["ok"] for c in calls))
        check("github was reached in the end", any("github" in c.data["tool"] for c in calls),
              str([c.data["tool"] for c in calls]))

        print("\nSCENARIO 5 — parallel dispatch must actually be parallel")
        # Three independent lookups in one turn. Without this the concurrency path in
        # dispatch.call_many never executes and would pass by never running.
        ev = await collect(agent, "List the open pull requests on acme/api-server, "
                                  "acme/web-client and acme/infra. Check all three.", name="5 parallel")
        finished = of(ev, "tool_call_finished")
        check("every call succeeded", finished and all(c.data["ok"] for c in finished),
              str([c.data["error"] for c in finished if not c.data["ok"]]))

        # Group by turn: calls dispatched together share a call_many batch.
        spans = sorted((c.data["started_at"], c.data["finished_at"]) for c in finished)
        overlaps = sum(1 for i in range(len(spans) - 1) if spans[i + 1][0] < spans[i][1])
        peak = max((sum(1 for s, f in spans if s <= t < f) for t, _ in spans), default=0)
        check("more than one tool call in the run", len(finished) >= 2, f"{len(finished)} calls")
        check("calls overlapped in time, so they ran concurrently", overlaps >= 1,
              f"{overlaps} overlapping pair(s), peak in-flight {peak}")
        check("peak concurrency above 1", peak >= 2, f"peak {peak}")
        check("concurrency stayed within the bound", peak <= config.TOOL_CONCURRENCY,
              f"peak {peak} <= {config.TOOL_CONCURRENCY}")

        print("\nSCENARIO 6 — iteration cap answers instead of erroring")
        original = config.MAX_ITERATIONS
        config.MAX_ITERATIONS = 1          # force the cap on a task that needs two turns
        try:
            ev = await collect(agent, "List the open PRs on acme/api-server, then post a "
                                      "summary of the newest to the eng Slack channel.", name="6 iteration cap")
        finally:
            config.MAX_ITERATIONS = original
        check("the cap fired", any(e.data.get("stage") == "iteration_cap" for e in of(ev, "error")))
        check("it still produced an answer", len(of(ev, "final_answer")) == 1)
        check("the answer is marked as capped",
              of(ev, "final_answer")[0].data.get("capped") is True)
        check("the run finished ok, not as an error page",
              of(ev, "done")[0].data["ok"] is True)

    print()
    print(f"model: {config.CHAT_MODEL}")
    print(f"{'scenario':<28} {'turns':>6} {'calls':>6} {'ms':>8}")
    for name, evs in runs:
        answer = of(evs, "final_answer")
        print(f"  {name:<26} {answer[0].data['turns'] if answer else '-':>6} "
              f"{len(of(evs, 'tool_call_finished')):>6} "
              f"{answer[0].data['duration_ms'] if answer else 0:>8.0f}")

    if failures:
        print(f"FAILED {len(failures)}: " + "; ".join(failures))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
