"""Connects every server in mcp.json, aggregates their tools, and survives one dying."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import tempfile
import time
from contextlib import AsyncExitStack
from typing import Any

import mcp.types as types
from mcp import Client, StdioServerParameters
from mcp.client.stdio import stdio_client
from pydantic import BaseModel

import config

log = logging.getLogger(__name__)


class AggregatedTool(BaseModel):
    server: str
    name: str
    description: str
    input_schema: dict[str, Any]

    # This property is the pool-wide unique name, since two servers may expose the same tool.
    @property
    def qualified_name(self) -> str:
        return f"{self.server}{config.TOOL_NAME_SEPARATOR}{self.name}"


class ServerStatus(BaseModel):
    name: str
    connected: bool
    tool_count: int
    error: str | None = None
    connect_attempts: int = 0


class CallResult(BaseModel):
    server: str
    tool: str
    ok: bool
    payload: Any = None
    error: str | None = None
    error_type: str | None = None
    duration_ms: float = 0.0


def load_manifest(path: Any = None) -> dict[str, dict[str, Any]]:
    # This function reads mcp.json in the Claude Desktop shape.
    raw = json.loads((path or config.MCP_MANIFEST).read_text())
    return raw["mcpServers"]


def _params(entry: dict[str, Any], env: dict[str, str] | None) -> StdioServerParameters:
    # This function turns one manifest entry into stdio launch parameters.
    command = entry["command"]
    # mcp.json says "python" because that is the shape Claude Desktop users recognise.
    # The pool runs the interpreter it was started with, so a venv works without editing it.
    if command in ("python", "python3"):
        command = sys.executable
    return StdioServerParameters(
        command=command,
        args=entry.get("args", []),
        cwd=str(config.ROOT),
        env=env,
    )


class _Connection:
    # This class owns one server's subprocess, its client, and its tool list.
    def __init__(self, name: str, entry: dict[str, Any], env: dict[str, str] | None) -> None:
        self.name = name
        self.entry = entry
        self.env = env
        self.client: Client | None = None
        self.stack: AsyncExitStack | None = None
        self.errlog: Any = None
        self.tools: list[AggregatedTool] = []
        self.error: str | None = None
        self.attempts = 0

    @property
    def connected(self) -> bool:
        return self.client is not None

    async def connect(self) -> bool:
        # This method starts the subprocess and lists its tools, recording failure rather than raising.
        self.attempts += 1
        stack = AsyncExitStack()
        # The subprocess explains itself on stderr. Capture it, or the trace only ever sees
        # the transport's generic "the pipe closed" wrapper. It has to be a real file: the
        # child inherits the descriptor, so an in-memory buffer has no fileno to hand over.
        errlog = tempfile.TemporaryFile("w+", encoding="utf-8", errors="replace")
        try:
            async with asyncio.timeout(config.SERVER_START_TIMEOUT_S):
                transport = stdio_client(_params(self.entry, self.env), errlog=errlog)
                client = await stack.enter_async_context(Client(transport))
                listed = await client.list_tools()
        except Exception as exc:
            await _quietly_close(stack)
            self.error = describe_failure(exc, _read_back(errlog))
            errlog.close()
            log.warning("server %s failed to connect: %s", self.name, self.error)
            return False

        self.stack = stack
        self.client = client
        self.errlog = errlog
        self.error = None
        self.tools = [
            AggregatedTool(server=self.name, name=t.name, description=t.description or "",
                           input_schema=t.input_schema or {})
            for t in listed.tools
        ]
        return True

    async def close(self) -> None:
        # This method tears the subprocess down and marks the connection unavailable.
        stack, self.stack, self.client, self.tools = self.stack, None, None, []
        if stack is not None:
            await _quietly_close(stack)
        if self.errlog is not None:
            self.errlog.close()
            self.errlog = None

    def status(self) -> ServerStatus:
        return ServerStatus(name=self.name, connected=self.connected,
                            tool_count=len(self.tools), error=self.error,
                            connect_attempts=self.attempts)


def _read_back(errlog: Any) -> str:
    # This function reads what the subprocess wrote to stderr before it died.
    try:
        errlog.seek(0)
        return errlog.read()
    except Exception:
        return ""


def _root_causes(exc: BaseException) -> list[BaseException]:
    # This function flattens ExceptionGroups, which otherwise hide the real cause from the trace.
    if isinstance(exc, BaseExceptionGroup):
        return [leaf for sub in exc.exceptions for leaf in _root_causes(sub)]
    return [exc]


def describe_failure(exc: BaseException, stderr: str = "") -> str:
    # This function turns a startup failure into a line worth putting in front of a person.
    causes = _root_causes(exc)
    parts = []
    for cause in causes:
        text = str(cause).strip()
        parts.append(f"{type(cause).__name__}: {text}" if text else type(cause).__name__)
    detail = "; ".join(dict.fromkeys(parts))

    tail = [ln.strip() for ln in stderr.strip().splitlines() if ln.strip()]
    if tail:
        detail = f"{detail} | server said: {tail[-1]}"
    return detail


async def _quietly_close(stack: AsyncExitStack) -> None:
    # This function closes a stack without letting teardown noise mask the original failure.
    try:
        await stack.aclose()
    except Exception as exc:
        log.debug("ignoring teardown error: %s", exc)


class ServerPool:
    """Connects the manifest's servers, aggregates tools/list, and dispatches tools/call."""

    def __init__(self, manifest: dict[str, dict[str, Any]] | None = None,
                 env: dict[str, str] | None = None,
                 reconnect_interval: float | None = None) -> None:
        self.manifest = manifest if manifest is not None else load_manifest()
        self.connections = {n: _Connection(n, e, env) for n, e in self.manifest.items()}
        self.reconnect_interval = reconnect_interval or config.SERVER_RECONNECT_INTERVAL_S
        self._reconnect_task: asyncio.Task[None] | None = None

    async def connect_all(self) -> list[ServerStatus]:
        # This method starts every server concurrently; failures degrade rather than raise.
        started = time.perf_counter()
        await asyncio.gather(*(c.connect() for c in self.connections.values()))
        elapsed = (time.perf_counter() - started) * 1000

        statuses = self.status()
        up = sum(s.connected for s in statuses)
        log.info("connected %d/%d servers, %d tools, %.0fms",
                 up, len(statuses), len(self.tools()), elapsed)
        return statuses

    def tools(self) -> list[AggregatedTool]:
        # This method returns the aggregated catalogue, excluding servers that are down.
        return [t for c in self.connections.values() if c.connected for t in c.tools]

    def status(self) -> list[ServerStatus]:
        # This method reports per-server health for the trace.
        return [c.status() for c in self.connections.values()]

    def resolve(self, qualified_name: str) -> tuple[str, str] | None:
        # This method splits a namespaced tool name into its server and tool.
        server, sep, tool = qualified_name.partition(config.TOOL_NAME_SEPARATOR)
        if not sep or server not in self.connections:
            return None
        return server, tool

    async def call(self, qualified_name: str, arguments: dict[str, Any]) -> CallResult:
        # This method dispatches one tools/call, returning failures as data.
        started = time.perf_counter()
        resolved = self.resolve(qualified_name)
        if resolved is None:
            return CallResult(server="?", tool=qualified_name, ok=False,
                              error=f"unknown tool {qualified_name!r}", error_type="UnknownTool")
        server, tool = resolved
        conn = self.connections[server]

        def elapsed() -> float:
            return (time.perf_counter() - started) * 1000

        if not conn.connected:
            return CallResult(server=server, tool=tool, ok=False, duration_ms=elapsed(),
                              error=f"server {server!r} is unavailable: {conn.error}",
                              error_type="ServerUnavailable")

        try:
            async with asyncio.timeout(config.TOOL_TIMEOUT_S):
                result = await conn.client.call_tool(tool, arguments)
        except asyncio.TimeoutError:
            return CallResult(server=server, tool=tool, ok=False, duration_ms=elapsed(),
                              error=f"timed out after {config.TOOL_TIMEOUT_S}s",
                              error_type="TimeoutError")
        except Exception as exc:
            # The subprocess died mid-call. Mark it down so the reconnect loop picks it up.
            await conn.close()
            conn.error = describe_failure(exc)
            return CallResult(server=server, tool=tool, ok=False, duration_ms=elapsed(),
                              error=conn.error, error_type=type(exc).__name__)

        return CallResult(server=server, tool=tool, ok=not result.is_error,
                          payload=_decode(result), duration_ms=elapsed(),
                          error=None if not result.is_error else _error_text(result),
                          error_type=None if not result.is_error else "ToolError")

    def start_reconnecting(self) -> None:
        # This method starts the background retry for servers that are down.
        if self._reconnect_task is None:
            self._reconnect_task = asyncio.create_task(self._reconnect_loop())

    async def _reconnect_loop(self) -> None:
        while True:
            await asyncio.sleep(self.reconnect_interval)
            down = [c for c in self.connections.values() if not c.connected]
            for conn in down:
                if await conn.connect():
                    log.info("server %s reconnected with %d tools", conn.name, len(conn.tools))

    async def aclose(self) -> None:
        # This method stops the reconnect loop and closes every subprocess.
        if self._reconnect_task is not None:
            self._reconnect_task.cancel()
            try:
                await self._reconnect_task
            except asyncio.CancelledError:
                pass
            self._reconnect_task = None
        for conn in self.connections.values():
            await conn.close()

    async def __aenter__(self) -> ServerPool:
        await self.connect_all()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()


def _text(result: types.CallToolResult) -> str:
    # This function pulls the text content out of a tool result.
    return "".join(b.text for b in result.content if isinstance(b, types.TextContent))


def _decode(result: types.CallToolResult) -> Any:
    # This function decodes a tool result's JSON payload, falling back to raw text.
    raw = _text(result)
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return raw


def _error_text(result: types.CallToolResult) -> str:
    # This function extracts the message from a failed tool result.
    payload = _decode(result)
    if isinstance(payload, dict) and "error" in payload:
        return str(payload["error"])
    return _text(result) or "tool reported an error with no message"
