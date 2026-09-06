"""FastAPI over the agent. Three endpoints, no agent logic here - handlers only stream."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import config
from agent.loop import Agent
from mcp_client.pool import ServerPool
from retrieval.index import ToolIndex, entries_from_pool

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
log = logging.getLogger(__name__)

state: dict[str, Any] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    # This function connects the servers and builds the index once, at startup.
    pool = ServerPool()
    await pool.connect_all()
    pool.start_reconnecting()
    index = ToolIndex(entries_from_pool(pool.tools()))
    state.update(pool=pool, index=index, agent=Agent(pool, index))
    log.info("ready: %d tools from %d servers", len(pool.tools()),
             sum(s.connected for s in pool.status()))
    try:
        yield
    finally:
        await pool.aclose()


app = FastAPI(title="mcp-tool-agent-orchestrator", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"])


class ChatRequest(BaseModel):
    query: str
    session_id: str | None = None


# Conversations, in memory, for the process lifetime. A follow-up question continues the
# session rather than starting over.
sessions: dict[str, list[dict[str, Any]]] = {}
# How many times each conversation has already stopped to ask, capped by MAX_CLARIFICATIONS.
clarifications: dict[str, int] = {}
# Tools retrieved so far in each conversation, so a follow-up does not re-retrieve them.
loaded: dict[str, list[Any]] = {}


def sse(data: dict[str, Any]) -> str:
    # This function formats one server-sent event.
    return f"data: {json.dumps(data, default=str)}\n\n"


@app.post("/chat")
async def chat(body: ChatRequest, request: Request) -> StreamingResponse:
    # This endpoint streams one run's trace. All the logic lives in Agent.run.
    request_id = uuid.uuid4().hex[:12]
    session_id = body.session_id or uuid.uuid4().hex[:12]
    # A session id the server has never seen means the client is holding a conversation this
    # process does not have - after a restart, say. Silently starting an empty history looks
    # to the user like the model forgot everything on screen, so it is reported instead.
    resumed = session_id in sessions
    history = sessions.setdefault(session_id, [])
    asked = clarifications.setdefault(session_id, 0)
    carried = loaded.setdefault(session_id, [])

    async def stream():
        log.info("[%s] session %s turn %d: %s", request_id, session_id,
                 sum(m["role"] == "user" for m in history) + 1, body.query[:100])
        yield sse({"type": "servers_connected", "ts": 0,
                   "data": {"servers": [s.model_dump() for s in state["pool"].status()],
                            "total_tools": len(state["pool"].tools()),
                            "request_id": request_id, "session_id": session_id,
                            "resumed": resumed or not body.session_id,
                            "turn_index": sum(m["role"] == "user" for m in history) + 1}})
        try:
            async for ev in state["agent"].run(body.query, history, asked, carried):
                if ev.type == "clarification_requested":
                    clarifications[session_id] = ev.data.get("asked", asked + 1)
                if await request.is_disconnected():
                    log.info("[%s] client disconnected", request_id)
                    return
                yield sse(ev.model_dump())
        except Exception as exc:
            # Nothing propagates as a 500: the stream has already started.
            log.exception("[%s] run failed", request_id)
            yield sse({"type": "error", "ts": 0,
                       "data": {"stage": "run", "error": f"{type(exc).__name__}: {exc}",
                                "request_id": request_id}})
            yield sse({"type": "done", "ts": 0, "data": {"ok": False}})

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@app.get("/servers")
async def servers() -> dict[str, Any]:
    # This endpoint reports per-server status and tool counts.
    pool: ServerPool = state["pool"]
    statuses = pool.status()
    return {
        "servers": [s.model_dump() for s in statuses],
        "connected": sum(s.connected for s in statuses),
        "total": len(statuses),
        "total_tools": len(pool.tools()),
        "indexed_tools": len(state["index"]),
        "router": {"retriever": config.ROUTER_RETRIEVER, "k": config.ROUTER_K,
                   "widen_k": config.WIDEN_K},
        "model": config.CHAT_MODEL,
    }


@app.post("/reset")
async def reset() -> dict[str, Any]:
    # This endpoint restores the seed world by restarting every server subprocess.
    #
    # The worlds live inside the subprocesses, so a restart is the reset. Rebuild the index
    # afterwards: a server that fails to come back must not leave stale tools in it.
    pool: ServerPool = state["pool"]
    await asyncio.gather(*(c.close() for c in pool.connections.values()))
    await pool.connect_all()
    index = ToolIndex(entries_from_pool(pool.tools()))
    state.update(index=index, agent=Agent(pool, index))
    sessions.clear()
    clarifications.clear()
    loaded.clear()
    log.info("reset: %d tools from %d servers, sessions cleared", len(pool.tools()),
             sum(s.connected for s in pool.status()))
    return {"ok": True, "total_tools": len(pool.tools()),
            "connected": sum(s.connected for s in pool.status())}


def main() -> None:
    # This function runs the API.
    import uvicorn
    uvicorn.run(app, host=config.HOST, port=config.PORT, log_level="info")


if __name__ == "__main__":
    main()
