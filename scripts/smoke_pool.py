"""Step 2 proof: 11 servers aggregate, one dying degrades rather than crashes, and it comes back."""

from __future__ import annotations

import asyncio
import collections
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from catalog.models import load_catalog  # noqa: E402
from mcp_client import dispatch  # noqa: E402
from mcp_client.pool import ServerPool, load_manifest  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    # This function records one assertion without aborting the rest of the run.
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not condition:
        failures.append(label)


async def main() -> int:
    catalog = load_catalog()
    expected = collections.Counter(t.service for t in catalog)

    print("\n1. all 11 servers connect and aggregate")
    started = time.perf_counter()
    async with ServerPool() as pool:
        startup_ms = (time.perf_counter() - started) * 1000
        statuses = pool.status()
        tools = pool.tools()
        per_server = collections.Counter(t.server for t in tools)

        check("11 servers connected", sum(s.connected for s in statuses) == 11,
              f"{sum(s.connected for s in statuses)}/11 in {startup_ms:.0f}ms")
        check(f"{len(catalog)} tools aggregate", len(tools) == len(catalog), f"got {len(tools)}")
        check("every server's count matches its catalogue slice", per_server == expected,
              f"got {dict(per_server)}")
        check("tool names are namespaced by server",
              all(t.qualified_name == f"{t.server}/{t.name}" for t in tools))
        check("schemas survive aggregation",
              all(t.input_schema.get("type") == "object" for t in tools))

        print("\n2. dispatch reaches real state on both connected servers")
        r = await dispatch.call(pool, "github/github_pulls_list",
                                {"owner": "acme", "repo": "api-server", "state": "open"})
        check("github_pulls_list succeeded", r.ok, r.error or "")
        pulls = r.payload.get("pull_requests", []) if r.ok else []
        check("two open PRs on acme/api-server", len(pulls) == 2, f"got {len(pulls)}")
        check("newest open PR is #482", pulls and pulls[0]["number"] == 482,
              str(pulls[0]["number"]) if pulls else "none")

        r = await dispatch.call(pool, "slack/slack_chat_post_message",
                                {"token": "xoxb", "channel": "eng", "text": "PR #482 needs review"})
        check("cross-server write succeeded", r.ok, r.error or "")
        posted_ts = r.payload.get("ts") if r.ok else None

        r = await dispatch.call(pool, "slack/slack_conversations_history",
                                {"token": "xoxb", "channel": "eng", "limit": 1})
        newest = r.payload.get("messages", [{}])[0] if r.ok else {}
        check("the write is readable back through the pool", newest.get("ts") == posted_ts,
              newest.get("text", ""))

        print("\n3. failures come back as data")
        r = await dispatch.call(pool, "github/github_pulls_list",
                                {"owner": "acme", "repo": "nope"})
        check("unknown repo is a tool error", not r.ok)
        check("the error lists real repos", "acme/api-server" in (r.error or ""), r.error or "")
        check("a validation error is not retried", len(r.attempts) == 1, f"{len(r.attempts)} attempts")

        r = await dispatch.call(pool, "nosuchserver/whatever", {})
        check("unknown server is an error, not a crash", not r.ok, r.error or "")

        r = await dispatch.call(pool, "twilio/twilio_list_service_conversation_message",
                                {"ChatServiceSid": "IS1", "ConversationSid": "CH1"})
        check("the injected validation failure fires", not r.ok, r.error or "")
        check("and is not retried", len(r.attempts) == 1, f"{len(r.attempts)} attempts")

        print("\n3b. the injected timeout retries, and the retry succeeds")
        started_retry = time.perf_counter()
        r = await dispatch.call(pool, "stripe/stripe_get_refunds", {"limit": 3})
        retry_ms = (time.perf_counter() - started_retry) * 1000
        check("two attempts were made", len(r.attempts) == 2, f"{len(r.attempts)} attempts")
        check("the first attempt timed out",
              r.attempts and r.attempts[0].error_type == "TimeoutError",
              r.attempts[0].error_type if r.attempts else "")
        check("the second attempt succeeded", r.ok, r.error or "")
        check("the connection survived the cancelled call", r.ok and r.payload is not None,
              f"{retry_ms:.0f}ms total")

    print("\n4. degraded path: a server that refuses to start")
    env = {**os.environ, "FAIL_SERVER_ON_START": "analytics"}
    async with ServerPool(env=env) as pool:
        statuses = {s.name: s for s in pool.status()}
        tools = pool.tools()
        check("10 of 11 servers connected", sum(s.connected for s in statuses.values()) == 10,
              f"{sum(s.connected for s in statuses.values())}/11")
        check("analytics is marked unavailable", not statuses["analytics"].connected)
        check("its failure is recorded for the trace", bool(statuses["analytics"].error),
              statuses["analytics"].error or "")
        check("the trace says why, not just that it failed",
              "FAIL_SERVER_ON_START" in (statuses["analytics"].error or ""))
        check("its 30 tools are dropped from the index",
              len(tools) == len(catalog) - expected["analytics"], f"got {len(tools)}")
        check("no analytics tools remain", not any(t.server == "analytics" for t in tools))

        r = await dispatch.call(pool, "analytics/anything", {})
        check("calling a downed server is an error, not a crash", not r.ok, r.error or "")
        check("the error says the server is unavailable",
              r.error_type == "ServerUnavailable", r.error_type or "")

        r = await dispatch.call(pool, "slack/slack_auth_test", {"token": "x"})
        check("the other servers keep serving", r.ok, r.error or "")

    print("\n5. background reconnect brings a dead server back")
    manifest = load_manifest()
    broken = {**manifest, "hr": {"command": "python", "args": ["-m", "servers.does_not_exist"]}}
    async with ServerPool(manifest=broken, reconnect_interval=0.5) as pool:
        check("hr failed to start", not pool.connections["hr"].connected,
              (pool.connections["hr"].error or "")[:60])
        check("its tools are absent", not any(t.server == "hr" for t in pool.tools()))

        # Repair the manifest entry, then let the background loop notice.
        pool.connections["hr"].entry = manifest["hr"]
        pool.start_reconnecting()
        for _ in range(20):
            await asyncio.sleep(0.5)
            if pool.connections["hr"].connected:
                break

        check("the reconnect loop brought hr back", pool.connections["hr"].connected)
        check("it took more than one attempt", pool.connections["hr"].attempts > 1,
              f"{pool.connections['hr'].attempts} attempts")
        hr_tools = [t for t in pool.tools() if t.server == "hr"]
        check("its tools are back in the index", len(hr_tools) == expected["hr"],
              f"got {len(hr_tools)}")
        if hr_tools:
            r = await dispatch.call(pool, hr_tools[0].qualified_name, {})
            check("and it answers calls again", r.error_type != "ServerUnavailable",
                  r.error or "ok")

    print()
    if failures:
        print(f"FAILED {len(failures)}: " + "; ".join(failures))
        return 1
    print(f"all checks passed · {len(catalog)} tools · startup {startup_ms:.0f}ms")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
