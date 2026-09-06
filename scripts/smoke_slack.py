"""Step 1 proof: launch the Slack server over stdio, list its tools, and exercise them."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp import Client, StdioServerParameters  # noqa: E402

TOKEN = "xoxb-mock"
CHANNEL = "C01ENG"
TS = "1757037600.000300"

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    # This function records one assertion without aborting the rest of the run.
    mark = "PASS" if condition else "FAIL"
    print(f"  [{mark}] {label}" + (f" — {detail}" if detail else ""))
    if not condition:
        failures.append(label)


async def call(client: Client, name: str, args: dict[str, Any]) -> tuple[bool, Any]:
    # This function calls a tool and decodes its JSON payload.
    result = await client.call_tool(name, args)
    payload = json.loads(result.content[0].text)
    return bool(result.is_error), payload


async def main() -> int:
    params = StdioServerParameters(command=sys.executable, args=["-m", "servers.slack"],
                                   cwd=str(Path(__file__).resolve().parent.parent))

    async with Client(params) as client:
        print("\n1. tools/list")
        listed = await client.list_tools()
        names = {t.name for t in listed.tools}
        check("42 tools aggregate from the catalogue slice", len(listed.tools) == 42,
              f"got {len(listed.tools)}")
        check("every tool carries an object schema",
              all(t.input_schema.get("type") == "object" for t in listed.tools))
        reactions_add = next(t for t in listed.tools if t.name == "slack_reactions_add")
        check("required fields survive into the schema",
              reactions_add.input_schema["required"] == ["channel", "name", "timestamp"])
        check("credentials are hidden from the schema",
              "token" not in reactions_add.input_schema["properties"])
        check("no advertised tool asks the model for a token",
              not any("token" in t.input_schema.get("properties", {}) for t in listed.tools))

        print("\n2. connected tool reads real seeded state")
        is_err, payload = await call(client, "slack_auth_test", {"token": TOKEN})
        check("slack_auth_test succeeded", not is_err, json.dumps(payload))
        check("identity comes from the world", payload.get("user") == "alice")

        is_err, payload = await call(client, "slack_conversations_info",
                                     {"token": TOKEN, "channel": CHANNEL, "include_num_members": True})
        check("slack_conversations_info succeeded", not is_err)
        check("channel #eng has 3 members", payload.get("channel", {}).get("num_members") == 3)

        print("\n3. write then read back — the state actually changed")
        is_err, before = await call(client, "slack_stars_list", {"token": TOKEN})
        check("slack_stars_list succeeded", not is_err)
        count_before = len(before.get("items", []))
        check("world seeded with one star", count_before == 1, f"got {count_before}")

        is_err, payload = await call(client, "slack_stars_add",
                                     {"token": TOKEN, "channel": CHANNEL, "timestamp": TS})
        check("slack_stars_add succeeded", not is_err, json.dumps(payload))

        is_err, after = await call(client, "slack_stars_list", {"token": TOKEN})
        items = after.get("items", [])
        check("star count went 1 -> 2", len(items) == count_before + 1, f"got {len(items)}")
        check("the new star is the message we starred",
              any(s["channel"] == CHANNEL and s["ts"] == TS for s in items))

        print("\n3b. the added tools carry scenario 2's Slack half")
        is_err, posted = await call(client, "slack_chat_post_message",
                                    {"token": TOKEN, "channel": "eng", "text": "PR #482 is ready"})
        check("slack_chat_post_message succeeded", not is_err, json.dumps(posted))
        is_err, history = await call(client, "slack_conversations_history",
                                     {"token": TOKEN, "channel": "eng", "limit": 1})
        check("slack_conversations_history succeeded", not is_err)
        newest = history.get("messages", [{}])[0]
        check("the posted message reads back", newest.get("text") == "PR #482 is ready",
              json.dumps(newest))
        check("read-back ts matches the write", newest.get("ts") == posted.get("ts"))

        print("\n4. errors come back as data, not exceptions")
        is_err, payload = await call(client, "slack_reactions_add",
                                     {"channel": CHANNEL, "name": "tada"})
        check("missing required argument is a tool error", is_err)
        check("the error names the missing argument", "timestamp" in payload.get("error", ""),
              payload.get("error", ""))

        # The credential is injected server-side, so omitting it is not an error at all.
        is_err, payload = await call(client, "slack_stars_list", {})
        check("a call with no token succeeds", not is_err, payload.get("error", ""))

        is_err, payload = await call(client, "slack_conversations_info",
                                     {"token": TOKEN, "channel": "C99NOPE"})
        check("unknown channel is a correctable error", is_err)
        check("the error lists valid channels", "eng" in payload.get("error", ""),
              payload.get("error", ""))

        is_err, payload = await call(client, "slack_stars_list", {"limit": "not-an-integer"})
        check("wrong argument type is rejected", is_err, payload.get("error", ""))

        is_err, payload = await call(client, "slack_auth_test", {"bogus": 1})
        check("unknown argument is rejected", is_err, payload.get("error", ""))

        result = await client.call_tool("slack_does_not_exist", {})
        check("unknown tool name is a tool error, not a crash", bool(result.is_error))

        print("\n5. synthetic tools are deterministic")
        target = "slack_team_access_logs"
        check("chosen synthetic tool is in the slice and unhandled", target in names)
        err_a, first = await call(client, target, {"token": TOKEN, "count": "5"})
        err_b, second = await call(client, target, {"token": TOKEN, "count": "5"})
        # Assert success first, or two identical errors would pass the determinism check.
        check("the synthetic call succeeded", not (err_a or err_b), json.dumps(first))
        check("identical arguments give identical results", first == second)
        err_c, different = await call(client, target, {"token": TOKEN, "count": "6"})
        check("different arguments give different results", not err_c and first != different)

        print("\n6. injected failure: slack_search_messages returns zero rows")
        is_err, payload = await call(client, "slack_search_messages",
                                     {"token": TOKEN, "query": "deploy"})
        check("the call succeeds", not is_err)
        check("but returns no rows", payload.get("count") == 0, json.dumps(payload))

    print()
    if failures:
        print(f"FAILED {len(failures)}: " + "; ".join(failures))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
