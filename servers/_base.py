"""Builds an MCP server from a slice of the catalogue: schemas, validation, handlers,  stdio."""

from __future__ import annotations

import hashlib
import json
import random
from collections.abc import Callable
from typing import Any

import anyio
import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

import config
from catalog.models import Tool, tools_for

Handler = Callable[[dict[str, Any]], Any]

_JSON_TYPES: dict[str, type | tuple[type, ...]] = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "array": list,
    "object": dict,
}


class ToolError(Exception):
    """A domain failure the model can correct. Returned as a tool result, never raised out."""


def is_credential(name: str) -> bool:
    # This function says whether a parameter is the server's business rather than the model's.
    return name in config.CREDENTIAL_PARAMS


def json_schema(tool: Tool) -> dict[str, Any]:
    # This function turns a catalogue tool's parameters into a JSON Schema, minus credentials.
    visible = [p for p in tool.parameters if not is_credential(p.name)]
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {p.name: {"type": p.type} for p in visible},
    }
    required = [p.name for p in visible if p.required]
    if required:
        schema["required"] = required
    return schema


def mcp_tools(tools: list[Tool]) -> list[types.Tool]:
    # This function converts catalogue tools into the MCP wire type.
    return [
        types.Tool(name=t.name, description=t.description, input_schema=json_schema(t))
        for t in tools
    ]


def validate(tool: Tool, args: dict[str, Any]) -> str | None:
    # This function checks arguments against the schema, returning a correctable message or None.
    by_name = {p.name: p for p in tool.parameters}

    missing = [p.name for p in tool.parameters if p.required and p.name not in args]
    if missing:
        return f"missing required argument(s): {', '.join(missing)}"

    unknown = sorted(k for k in args if k not in by_name)
    if unknown:
        return (
            f"unknown argument(s): {', '.join(unknown)}. "
            f"valid arguments are: {', '.join(sorted(by_name))}"
        )

    for name, value in args.items():
        declared = by_name[name].type
        expected = _JSON_TYPES.get(declared)
        if expected is None:
            continue
        # bool is a subclass of int in Python, but not a number in JSON Schema.
        if declared in ("integer", "number") and isinstance(value, bool):
            return f"argument '{name}' must be {declared}, got boolean"
        if not isinstance(value, expected):
            return f"argument '{name}' must be {declared}, got {type(value).__name__}"
    return None


def _seed(tool_name: str, args: dict[str, Any]) -> int:
    # This function derives a stable seed from the call, so identical calls return identical data.
    key = f"{config.SEED}:{tool_name}:{json.dumps(args, sort_keys=True, default=str)}"
    return int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "big")


def _field_value(param_name: str, param_type: str, rng: random.Random) -> Any:
    # This function invents one plausible field value from a parameter's name and type.
    if param_type == "boolean":
        return rng.choice([True, False])
    if param_type == "integer":
        return rng.randint(1, 500)
    if param_type == "number":
        return round(rng.uniform(1, 500), 2)
    if param_type == "array":
        return []
    if param_type == "object":
        return {}
    return f"{param_name}_{rng.randrange(16**6):06x}"


def _is_list_shaped(tool: Tool) -> bool:
    # This function guesses whether a tool returns a collection, from its name and description.
    haystack = f"{tool.name} {tool.description}".lower()
    return any(w in haystack for w in ("list", "search", "retrieve a list", "lists"))


def synthetic_result(tool: Tool, args: dict[str, Any]) -> dict[str, Any]:
    # This function builds a deterministic schema-derived response for an unimplemented tool.
    rng = random.Random(_seed(tool.name, args))
    record = {p.name: _field_value(p.name, p.type, rng) for p in tool.parameters}
    record["id"] = f"{tool.service[:2]}_{rng.randrange(16**8):08x}"

    if _is_list_shaped(tool):
        count = rng.randint(1, 3)
        items = [record] + [
            {**{p.name: _field_value(p.name, p.type, rng) for p in tool.parameters},
             "id": f"{tool.service[:2]}_{rng.randrange(16**8):08x}"}
            for _ in range(count - 1)
        ]
        return {"ok": True, "synthetic": True, "count": count, "items": items}
    return {"ok": True, "synthetic": True, **record}


def ok(payload: Any) -> types.CallToolResult:
    # This function wraps a successful payload as a tool result.
    text = payload if isinstance(payload, str) else json.dumps(payload, default=str)
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)])


def err(message: str) -> types.CallToolResult:
    # This function returns a failure as data, so the model can correct it.
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps({"ok": False, "error": message}))],
        is_error=True,
    )


def build_server(service: str, handlers: dict[str, Handler] | None = None) -> Server:
    # This function builds an MCP server for one service from its catalogue slice.
    if config.FAIL_SERVER_ON_START == service:
        raise SystemExit(f"{service}: refusing to start (FAIL_SERVER_ON_START)")

    tools = tools_for(service)
    by_name = {t.name: t for t in tools}
    wire = mcp_tools(tools)
    handlers = handlers or {}

    unknown_handlers = sorted(set(handlers) - set(by_name))
    if unknown_handlers:
        raise ValueError(f"{service}: handlers for tools not in the catalogue: {unknown_handlers}")

    attempts: dict[str, int] = {}

    async def on_list_tools(ctx: Any, params: Any) -> types.ListToolsResult:
        return types.ListToolsResult(tools=wire)

    async def on_call_tool(ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        return await _call(params.name, params.arguments or {})

    async def _call(name: str, args: dict[str, Any]) -> types.CallToolResult:
        tool = by_name.get(name)
        if tool is None:
            return err(f"unknown tool {name!r} on server {service!r}")

        # The server supplies its own credentials. A model that has to invent a token either
        # fabricates one or correctly refuses to; neither is the model's problem to solve.
        args = dict(args)
        for param in tool.parameters:
            if is_credential(param.name) and param.name not in args:
                args[param.name] = f"mock-{service}-{param.name}"

        problem = validate(tool, args)
        if problem:
            return err(problem)

        flaky = await _apply_flaky(tool, args, attempts)
        if flaky is not None:
            return flaky

        handler = handlers.get(name)
        if handler is None:
            return ok(synthetic_result(tool, args))
        try:
            return ok(handler(args))
        except ToolError as exc:
            return err(str(exc))

    return Server(
        service,
        version="0.1.0",
        instructions=f"Mock {service} MCP server. {len(tools)} tools from the catalogue.",
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )


async def _apply_flaky(
    tool: Tool, args: dict[str, Any], attempts: dict[str, int]
) -> types.CallToolResult | None:
    # This function applies a tool's configured failure mode, or returns None to proceed normally.
    mode = config.FLAKY_TOOLS.get(tool.name)
    if mode is None:
        return None

    if mode == "empty":
        return ok({"ok": True, "count": 0, "items": []})

    if mode == "validation":
        return err(
            f"{tool.name} rejected its arguments: this endpoint requires a valid "
            f"resource sid for {', '.join(p.name for p in tool.parameters if p.required)}"
        )

    if mode == "timeout":
        key = f"{tool.name}:{json.dumps(args, sort_keys=True, default=str)}"
        attempts[key] = attempts.get(key, 0) + 1
        if attempts[key] == 1:
            await anyio.sleep(config.TOOL_TIMEOUT_S + 1)
        return None

    raise ValueError(f"unknown failure mode {mode!r} for {tool.name}")


def run_stdio(server: Server) -> None:
    # This function serves one server over stdio, the way Claude Desktop launches it.
    async def main() -> None:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    anyio.run(main)
