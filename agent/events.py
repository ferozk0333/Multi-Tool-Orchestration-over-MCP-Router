"""The trace. Every step of a run emits one of these, and the UI renders them in order."""

from __future__ import annotations

import time
from typing import Any, Literal

from pydantic import BaseModel, Field

EventType = Literal[
    "servers_connected", "routing_started", "tools_selected",
    "turn_started", "tool_call_started", "tool_call_finished",
    "tools_widened", "clarification_requested",
    "server_unavailable", "final_answer", "error", "done",
]


class Event(BaseModel):
    type: EventType
    ts: float = Field(default_factory=time.time)
    data: dict[str, Any] = {}


def event(type_: EventType, **data: Any) -> Event:
    # This function builds one trace event.
    return Event(type=type_, data=data)
