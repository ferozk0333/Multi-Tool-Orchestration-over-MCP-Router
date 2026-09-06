"""The agent loop: route, call the model, dispatch tools, widen on refusal, cap the turns."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator
from typing import Any

import anthropic

import config
from agent.events import Event, event
from agent.prompts import SYSTEM
from agent.schemas import ASK_CLARIFICATION, REQUEST_MORE_TOOLS, ToolBox
from mcp_client import dispatch
from mcp_client.pool import ServerPool
from retrieval.base import Hit
from retrieval.index import ToolIndex
from retrieval.router import Routing, estimate_tokens, route, widen

log = logging.getLogger(__name__)


def _summarise(payload: Any, limit: int = 200) -> str:
    # This function shortens a tool result for the trace without hiding that it was truncated.
    text = json.dumps(payload, default=str) if not isinstance(payload, str) else payload
    return text if len(text) <= limit else text[:limit] + f"… (+{len(text) - limit} chars)"


def _result_text(result: dispatch.DispatchResult) -> str:
    # This function renders a tool result for the model, errors included as data.
    if result.ok:
        return json.dumps(result.payload, default=str)
    return json.dumps({"ok": False, "error": result.error, "error_type": result.error_type})


class Agent:
    """One conversation. Owns the loop, emits the trace."""

    def __init__(self, pool: ServerPool, index: ToolIndex,
                 client: anthropic.AsyncAnthropic | None = None) -> None:
        self.pool = pool
        self.index = index
        self.client = client or anthropic.AsyncAnthropic()

    async def run(self, query: str,
                  messages: list[dict[str, Any]] | None = None,
                  asked: int = 0,
                  carried: list[Any] | None = None) -> AsyncIterator[Event]:
        # This method runs one query to an answer, yielding every step as it happens.
        #
        # `messages` is the conversation so far and is appended to in place, so a caller that
        # holds one list across turns gets a real multi-turn conversation.
        started = time.perf_counter()
        if messages is None:
            messages = []
        messages.append({"role": "user", "content": query})

        # Start with no catalogue tools at all - just the two synthetic ones. A question the
        # model can answer from the conversation ("what did you just do?") should cost no
        # retrieval and no schemas, and only the model knows whether it needs data. Asking it
        # is the same move as request_more_tools and ask_clarification: the model says so
        # explicitly rather than the system guessing from the outside.
        # Once the conversation has stopped to ask MAX_CLARIFICATIONS times, the tool is no
        # longer offered and the model has to proceed on best effort rather than ping-ponging.
        may_ask = asked < config.MAX_CLARIFICATIONS

        # Tools already retrieved in this conversation stay loaded. Starting empty on every
        # message made a follow-up rediscover the same toolbox: answering "on acme/api-server"
        # after a clarifying question cost two more retrievals and six turns, for tools the
        # previous turn had already found.
        carried = carried if carried is not None else []
        box = ToolBox(list(carried), allow_clarify=may_ask)
        routed, widens, current_k = bool(carried), 0, len(carried)
        # Whether retrieval actually ran this turn, as opposed to reusing carried tools. The
        # trace says "no retrieval needed" off this, so it must not conflate the two.
        retrieved_now = False
        for turn in range(1, config.MAX_ITERATIONS + 1):
            yield event("turn_started", turn=turn, tools_available=len(box))

            try:
                response = await self.client.messages.create(
                    model=config.CHAT_MODEL, max_tokens=config.MAX_RESPONSE_TOKENS,
                    system=SYSTEM, messages=messages, tools=box.schemas,
                )
            except Exception as exc:
                yield event("error", stage="model_call", turn=turn,
                            error=f"{type(exc).__name__}: {exc}")
                yield event("done", turns=turn, ok=False)
                return

            calls = [b for b in response.content if b.type == "tool_use"]
            text = "".join(b.text for b in response.content if b.type == "text").strip()

            asking = next((c for c in calls if c.name == ASK_CLARIFICATION), None)
            if asking is not None:
                question = (asking.input or {}).get("question", "").strip() or text
                # Record the question as the assistant's turn so the user's reply has context.
                messages.append({"role": "assistant", "content": question})
                yield event("clarification_requested", question=question, turn=turn,
                            tool_calls=0, asked=asked + 1,
                            remaining=config.MAX_CLARIFICATIONS - asked - 1)
                yield event("final_answer", text=question, turns=turn, clarification=True,
                            routed=retrieved_now, tools_loaded=len(box),
                            duration_ms=(time.perf_counter() - started) * 1000,
                            input_tokens=response.usage.input_tokens,
                            output_tokens=response.usage.output_tokens)
                yield event("done", turns=turn, ok=True)
                return

            if not calls:
                messages.append({"role": "assistant", "content": response.content})
                yield event("final_answer", text=text, turns=turn, routed=retrieved_now,
                            tools_loaded=len(box),
                            duration_ms=(time.perf_counter() - started) * 1000,
                            input_tokens=response.usage.input_tokens,
                            output_tokens=response.usage.output_tokens)
                yield event("done", turns=turn, ok=True)
                return

            messages.append({"role": "assistant", "content": response.content})

            more = next((c for c in calls if c.name == REQUEST_MORE_TOOLS), None)
            if more is not None:
                needed = (more.input or {}).get("needed", "")
                if not routed:
                    # First request: this is the initial routing, not a widen. Retrieving on
                    # the model's stated need is also better than retrieving on the raw
                    # question - measured, on the cross-server case.
                    yield event("routing_started", query=query, need=needed,
                                k=config.ROUTER_K, retriever=config.ROUTER_RETRIEVER,
                                total_tools=len(self.index))
                    routing = route(self.index, f"{needed} {query}".strip())
                    yield self._tools_selected(routing)
                    box, current_k, routed = ToolBox(routing.tools, may_ask), routing.k, True
                    carried[:] = routing.tools
                    retrieved_now = True
                elif widens < config.MAX_WIDENS:
                    # Retrieve on what the model said it needs, joined to the original
                    # question. The need alone finds the missing half but loses the tools for
                    # the other half of a cross-server task; the original query alone cannot
                    # find the missing half at any k, which is why widening on it failed.
                    wider = widen(self.index, f"{needed} {query}".strip())
                    merged = self._merge(wider, box)
                    yield event("tools_widened", reason=needed, old_k=current_k,
                                new_k=merged.k, servers=merged.servers(),
                                carried_over=merged.k - wider.k)
                    yield self._tools_selected(merged)
                    box, current_k, widens = ToolBox(merged.tools, may_ask), merged.k, widens + 1
                    carried[:] = merged.tools
                    retrieved_now = True
                else:
                    yield event("tools_widened", reason=needed, refused=True,
                                old_k=current_k, new_k=current_k,
                                note=f"already widened {widens}, cap is {config.MAX_WIDENS}")
                messages.append({"role": "user",
                                 "content": self._widen_results(calls, more, box)})
                continue

            results = []
            async for ev, result in self._dispatch(calls, box, turn):
                yield ev
                if result is not None:
                    results.append(result)
            messages.append({"role": "user", "content": results})

        # Iteration cap. Answer from what was gathered rather than erroring out.
        async for ev in self._answer_from_partial(messages, started):
            yield ev

    def _merge(self, wider: Routing, box: ToolBox) -> Routing:
        # This method keeps every tool the model already had, on top of the re-ranked wider set.
        #
        # Widening is supposed to add capability. Re-ranking at a larger k on a different query
        # can drop tools the smaller cut had: on the cross-server scenario, the original 12
        # contained slack_chat_post_message at rank 8 and widening for GitHub removed it, so
        # the model found the PR and then had no way to post it. Widening only ever adds now.
        fresh = {t.key for t in wider.tools}
        kept = [e for e in box.entries if e.key not in fresh]
        tools = wider.tools + kept
        hits = wider.hits + [
            Hit(tool_key=e.key, score=0.0, rank=len(wider.hits) + i + 1)
            for i, e in enumerate(kept)
        ]
        return Routing(
            query=wider.query, k=len(tools), tools=tools, hits=hits, widened=True,
            duration_ms=wider.duration_ms,
            estimated_tokens_selected=estimate_tokens(tools),
            estimated_tokens_all=wider.estimated_tokens_all,
        )

    def _tools_selected(self, routing: Routing) -> Event:
        # This method reports the routed subset and what it saved.
        return event(
            "tools_selected", k=routing.k, widened=routing.widened,
            servers=routing.servers(), duration_ms=round(routing.duration_ms, 1),
            tools=[{"key": t.key, "score": round(h.score, 4), "rank": h.rank}
                   for t, h in zip(routing.tools, routing.hits)],
            estimated_tokens_selected=routing.estimated_tokens_selected,
            estimated_tokens_all=routing.estimated_tokens_all,
            estimated_tokens_saved=routing.estimated_tokens_saved,
        )

    def _widen_results(self, calls: list[Any], more: Any, box: ToolBox) -> list[dict[str, Any]]:
        # This method answers every tool_use block, since the API requires one result each.
        out = []
        for call in calls:
            if call.id == more.id:
                out.append({"type": "tool_result", "tool_use_id": call.id,
                            "content": f"{len(box)} tools are now loaded. "
                                       f"Re-read the tool list and continue."})
            else:
                out.append({"type": "tool_result", "tool_use_id": call.id,
                            "content": "Not run: the tool set changed this turn. Reconsider this call."})
        return out

    async def _dispatch(self, calls: list[Any], box: ToolBox, turn: int):
        # This method runs a turn's tool calls concurrently and reports each one.
        planned = [box.resolve(c.name, c.input or {}) for c in calls]
        for call, (name, args) in zip(calls, planned):
            yield event("tool_call_started", id=call.id, tool=name, turn=turn,
                        server=name.split("/")[0] if box.knows(call.name) else "?",
                        arguments=args, batch=len(calls)), None

        # A name the model invented never reaches the pool: answer it here, where the tools
        # offered this turn are known, so the reply can name them.
        invented = {i for i, c in enumerate(calls) if not box.knows(c.name)}
        real = [p for i, p in enumerate(planned) if i not in invented]
        done = iter(await dispatch.call_many(self.pool, real))
        results = [
            dispatch.DispatchResult(
                server="?", tool=calls[i].name, ok=False, error_type="UnknownTool",
                error=box.unknown_tool_error(calls[i].name),
            ) if i in invented else next(done)
            for i in range(len(calls))
        ]

        for call, result in zip(calls, results):
            yield event(
                "tool_call_finished", id=call.id, turn=turn,
                tool=f"{result.server}/{result.tool}",
                ok=result.ok, attempts=len(result.attempts),
                duration_ms=round(result.duration_ms, 1),
                summary=_summarise(result.payload) if result.ok else None,
                error=result.error, error_type=result.error_type,
                # Monotonic bounds, so overlap across a turn's calls is visible in the trace
                # itself rather than needing test-only instrumentation.
                started_at=result.started_at, finished_at=result.finished_at,
            ), {"type": "tool_result", "tool_use_id": call.id,
                "content": _result_text(result), "is_error": not result.ok}

    async def _answer_from_partial(self, messages: list[dict[str, Any]], started: float):
        # This method produces an answer after the iteration cap, from whatever was gathered.
        yield event("error", stage="iteration_cap", max_iterations=config.MAX_ITERATIONS,
                    note="answering from what was gathered")
        messages.append({"role": "user", "content":
                         "Stop calling tools. Answer now from what you have already gathered, "
                         "and say plainly what you could not determine."})
        try:
            response = await self.client.messages.create(
                model=config.CHAT_MODEL, max_tokens=config.MAX_RESPONSE_TOKENS,
                system=SYSTEM, messages=messages,
            )
            text = "".join(b.text for b in response.content if b.type == "text").strip()
            messages.append({"role": "assistant", "content": response.content})
        except Exception as exc:
            text = f"Hit the iteration cap and could not summarise: {type(exc).__name__}: {exc}"

        yield event("final_answer", text=text, turns=config.MAX_ITERATIONS, capped=True,
                    duration_ms=(time.perf_counter() - started) * 1000)
        yield event("done", turns=config.MAX_ITERATIONS, ok=True, capped=True)
