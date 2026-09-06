"""tools/call with conditional retry. Transient failures retry, deterministic ones do not."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from pydantic import BaseModel

import config
from mcp_client.pool import CallResult, ServerPool

log = logging.getLogger(__name__)


class Attempt(BaseModel):
    n: int
    ok: bool
    error: str | None = None
    error_type: str | None = None
    duration_ms: float


class DispatchResult(BaseModel):
    server: str
    tool: str
    ok: bool
    payload: Any = None
    error: str | None = None
    error_type: str | None = None
    attempts: list[Attempt] = []
    # Monotonic wall-clock bounds of the whole call. Overlapping intervals across results are
    # what proves a turn's calls actually ran in parallel rather than in sequence.
    started_at: float = 0.0
    finished_at: float = 0.0

    @property
    def duration_ms(self) -> float:
        return sum(a.duration_ms for a in self.attempts)


def is_retryable(result: CallResult) -> bool:
    # This function decides whether a failure is worth a second attempt.
    # A deterministic failure fails identically every time, so retrying only burns latency.
    return result.error_type in config.RETRYABLE_EXCEPTIONS


async def call(pool: ServerPool, qualified_name: str, arguments: dict[str, Any],
               max_retries: int | None = None) -> DispatchResult:
    # This function calls one tool, retrying only transient failures.
    budget = config.TOOL_MAX_RETRIES if max_retries is None else max_retries
    attempts: list[Attempt] = []
    result: CallResult | None = None
    started_at = time.monotonic()

    for n in range(1, budget + 2):
        result = await pool.call(qualified_name, arguments)
        attempts.append(Attempt(n=n, ok=result.ok, error=result.error,
                                error_type=result.error_type, duration_ms=result.duration_ms))
        if result.ok or not is_retryable(result):
            break
        if n <= budget:
            log.info("retrying %s after %s (attempt %d of %d)",
                     qualified_name, result.error_type, n, budget + 1)
            await asyncio.sleep(0.1 * n)

    assert result is not None
    return DispatchResult(server=result.server, tool=result.tool, ok=result.ok,
                          payload=result.payload, error=result.error,
                          error_type=result.error_type, attempts=attempts,
                          started_at=started_at, finished_at=time.monotonic())


async def call_many(pool: ServerPool, calls: list[tuple[str, dict[str, Any]]]) -> list[DispatchResult]:
    # This function runs a turn's tool calls with bounded concurrency.
    limit = asyncio.Semaphore(config.TOOL_CONCURRENCY)

    async def one(name: str, args: dict[str, Any]) -> DispatchResult:
        async with limit:
            return await call(pool, name, args)

    return await asyncio.gather(*(one(name, args) for name, args in calls))
