"""Typed access to the committed tool catalogue."""

from __future__ import annotations

import json
from functools import lru_cache

from pydantic import BaseModel

import config


class Parameter(BaseModel):
    name: str
    type: str
    required: bool = False


class Tool(BaseModel):
    name: str
    description: str
    parameters: list[Parameter]
    service: str
    source: str
    spec_path: str | None = None
    http_method: str | None = None

    # This property is the text every retriever indexes, per RETRIEVAL.md.
    @property
    def indexed_text(self) -> str:
        return f"{self.name} {self.description} {' '.join(p.name for p in self.parameters)}"


@lru_cache(maxsize=1)
def load_catalog() -> tuple[Tool, ...]:
    # This function loads and validates every tool in the catalogue.
    raw = json.loads(config.CATALOG_PATH.read_text())
    return tuple(Tool.model_validate(t) for t in raw["tools"])


def tools_for(service: str) -> list[Tool]:
    # This function returns one service's slice of the catalogue.
    slice_ = [t for t in load_catalog() if t.service == service]
    if not slice_:
        raise ValueError(f"no tools in catalogue for service {service!r}")
    return slice_
