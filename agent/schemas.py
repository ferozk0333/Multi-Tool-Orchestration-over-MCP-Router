"""Turns routed tools into Anthropic tool schemas, and translates the model's names back."""

from __future__ import annotations

import re
from typing import Any

from retrieval.base import IndexEntry

# The API's allowed property-key pattern. Twilio's real spec has parameters named
# "DateCreated<" and "DateCreated>", which are legal OpenAPI and illegal here, so a schema
# built straight from the catalogue is rejected outright.
_ILLEGAL = re.compile(r"[^a-zA-Z0-9_.-]")

REQUEST_MORE_TOOLS = "request_more_tools"
ASK_CLARIFICATION = "ask_clarification"

# Also never dispatched. Sniffing the answer text for a trailing "?" looked like it worked and
# then missed a real clarifying question that ended in ")". Detecting intent from outside the
# model is the thing this design rejects, so the model says it explicitly here too.
ASK_CLARIFICATION_SCHEMA: dict[str, Any] = {
    "name": ASK_CLARIFICATION,
    "description": (
        "Call this when the request is genuinely ambiguous - an unnamed person, file, channel "
        "or record you cannot resolve from the tools - and you need the user to answer before "
        "any tool call would be meaningful. Call no other tool in the same turn."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "One short question for the user."},
        },
        "required": ["question"],
    },
}

# Not in any server's catalogue and never dispatched. It exists so the model can say "wrong
# toolbox" out loud, because a model given the wrong tools usually calls one anyway.
REQUEST_MORE_TOOLS_SCHEMA: dict[str, Any] = {
    "name": REQUEST_MORE_TOOLS,
    "description": (
        "Call this when none of the available tools can answer the request, or when the tools "
        "you have belong to the wrong service. Do not guess with a tool that merely looks "
        "similar. Say what capability you actually need."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "needed": {"type": "string",
                       "description": "The capability you need, in your own words."},
        },
        "required": ["needed"],
    },
}


def safe_property(name: str) -> str:
    # This function makes one parameter name legal as an API schema key.
    return _ILLEGAL.sub("_", name)


def tool_schema(entry: IndexEntry) -> tuple[dict[str, Any], dict[str, str]]:
    # This function builds one tool schema and the map from safe names back to real ones.
    renames: dict[str, str] = {}
    properties: dict[str, Any] = {}
    for param, spec in entry.properties.items():
        safe = safe_property(param)
        if safe != param:
            renames[safe] = param
        # Carry the real type through. Declaring everything as a string makes the model send
        # "100" for an integer field, which the server then rejects as a validation error.
        properties[safe] = dict(spec) if isinstance(spec, dict) else {"type": "string"}

    input_schema: dict[str, Any] = {"type": "object", "properties": properties}
    required = [safe_property(r) for r in entry.required if r in entry.properties]
    if required:
        input_schema["required"] = required

    schema = {
        # The model addresses tools by the pool's qualified name, so two servers exposing the
        # same tool name stay distinguishable. "/" is not legal in a tool name, so use "__".
        "name": entry.key.replace("/", "__"),
        "description": entry.description or entry.name,
        "input_schema": input_schema,
    }
    return schema, renames


class ToolBox:
    """The tools one turn shows the model, plus what is needed to dispatch their calls."""

    def __init__(self, entries: list[IndexEntry], allow_clarify: bool = True) -> None:
        self.entries = entries
        self.schemas: list[dict[str, Any]] = []
        self.renames: dict[str, dict[str, str]] = {}
        self.qualified: dict[str, str] = {}

        for entry in entries:
            schema, renames = tool_schema(entry)
            self.schemas.append(schema)
            self.qualified[schema["name"]] = entry.key
            if renames:
                self.renames[schema["name"]] = renames
        self.schemas.append(REQUEST_MORE_TOOLS_SCHEMA)
        if allow_clarify:
            self.schemas.append(ASK_CLARIFICATION_SCHEMA)

    def resolve(self, model_name: str, arguments: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        # This method maps a model tool call back to a pool tool name and its real arguments.
        renames = self.renames.get(model_name, {})
        args = {renames.get(k, k): v for k, v in arguments.items()}
        return self.qualified.get(model_name, model_name), args

    def knows(self, model_name: str) -> bool:
        # This method says whether a name is one of the tools actually offered this turn.
        return model_name in self.qualified

    def unknown_tool_error(self, model_name: str) -> str:
        # This function tells the model what to do instead of guessing another name.
        #
        # A bare "unknown tool" invites another guess, and the model will spend turns
        # inventing plausible API names for a capability the catalogue may simply not have.
        # Naming the closest tools it *does* have, and the way to ask for more, ends that loop.
        stem = model_name.split("__")[-1].strip("_")
        words = {w for w in stem.split("_") if len(w) > 2}
        close = [n for n in self.qualified if words & set(n.split("__")[-1].split("_"))]
        hint = f" Closest available: {', '.join(sorted(close)[:5])}." if close else ""
        return (
            f"No tool named {model_name!r} is loaded. Do not guess another name."
            f"{hint} If the capability you need is not in your tool list, call "
            f"{REQUEST_MORE_TOOLS} and describe it."
        )

    def __len__(self) -> int:
        return len(self.entries)
