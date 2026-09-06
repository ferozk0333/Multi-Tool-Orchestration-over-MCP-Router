"""Slack MCP server: 40 catalogue tools, nine of them wired to real world state."""

from __future__ import annotations

from typing import Any

from mcp.server.lowlevel import Server

from servers._base import ToolError, build_server, run_stdio
from servers._world import AUTHED_USER, SlackWorld

WORLD = SlackWorld()


def _require_channel(args: dict[str, Any]) -> dict[str, Any]:
    # This function resolves the channel argument or raises a message the model can act on.
    ref = args.get("channel")
    if not ref:
        raise ToolError(f"channel is required. known channels: {', '.join(WORLD.channel_names())}")
    channel = WORLD.channel_by_ref(ref)
    if channel is None:
        raise ToolError(f"channel_not_found: {ref!r}. known channels: {', '.join(WORLD.channel_names())}")
    return channel


def auth_test(args: dict[str, Any]) -> dict[str, Any]:
    # This function reports the identity the token belongs to.
    team = WORLD.state["team"]
    user = WORLD.state["users"][AUTHED_USER]
    return {
        "ok": True,
        "url": f"https://{team['domain']}.slack.com/",
        "team": team["name"],
        "team_id": team["id"],
        "user": user["name"],
        "user_id": user["id"],
    }


def conversations_info(args: dict[str, Any]) -> dict[str, Any]:
    # This function returns one channel's record.
    channel = _require_channel(args)
    record = {k: v for k, v in channel.items() if k != "members"}
    if args.get("include_num_members"):
        record["num_members"] = len(channel["members"])
    return {"ok": True, "channel": record}


def users_profile_get(args: dict[str, Any]) -> dict[str, Any]:
    # This function returns a user's profile, defaulting to the authenticated user.
    user_id = args.get("user", AUTHED_USER)
    user = WORLD.state["users"].get(user_id)
    if user is None:
        known = ", ".join(sorted(WORLD.state["users"]))
        raise ToolError(f"user_not_found: {user_id!r}. known user ids: {known}")
    return {
        "ok": True,
        "profile": {
            "real_name": user["real_name"],
            "display_name": user["name"],
            "title": user["title"],
            "tz": user["tz"],
        },
    }


def chat_post_message(args: dict[str, Any]) -> dict[str, Any]:
    # This function appends a message to a channel, which conversations_history reads back.
    channel = _require_channel(args)
    text = args.get("text")
    if not text and not args.get("blocks") and not args.get("attachments"):
        raise ToolError("no_text: one of text, blocks or attachments is required")

    messages = WORLD.state["messages"].setdefault(channel["id"], [])
    last = float(messages[-1]["ts"]) if messages else 1757000000.0
    ts = f"{last + 3600:.6f}"
    message = {"ts": ts, "user": AUTHED_USER, "text": text or "", "bot": True}
    messages.append(message)
    return {"ok": True, "channel": channel["id"], "ts": ts, "message": message}


def conversations_history(args: dict[str, Any]) -> dict[str, Any]:
    # This function returns a channel's messages, newest first.
    channel = _require_channel(args)
    messages = sorted(WORLD.state["messages"].get(channel["id"], []),
                      key=lambda m: float(m["ts"]), reverse=True)
    limit = args.get("limit")
    if limit is not None:
        messages = messages[:limit]
    return {"ok": True, "messages": messages, "has_more": False}


def stars_add(args: dict[str, Any]) -> dict[str, Any]:
    # This function stars a message and persists it in the world.
    channel = _require_channel(args)
    ts = args.get("timestamp")
    if not ts:
        raise ToolError("timestamp is required to star a message")
    if WORLD.message(channel["id"], ts) is None:
        known = ", ".join(m["ts"] for m in WORLD.state["messages"][channel["id"]])
        raise ToolError(f"message_not_found: no message at {ts} in #{channel['name']}. timestamps: {known}")

    star = {"type": "message", "channel": channel["id"], "ts": ts, "user": AUTHED_USER}
    if star in WORLD.state["stars"]:
        raise ToolError("already_starred")
    WORLD.state["stars"].append(star)
    return {"ok": True}


def stars_list(args: dict[str, Any]) -> dict[str, Any]:
    # This function lists the authenticated user's stars, reading back what stars_add wrote.
    items = [s for s in WORLD.state["stars"] if s["user"] == AUTHED_USER]
    return {"ok": True, "items": items, "paging": {"count": len(items), "total": len(items), "page": 1}}


def reactions_add(args: dict[str, Any]) -> dict[str, Any]:
    # This function adds an emoji reaction to a message.
    channel = _require_channel(args)
    ts = args["timestamp"]
    if WORLD.message(channel["id"], ts) is None:
        raise ToolError(f"message_not_found: no message at {ts} in #{channel['name']}")

    key = f"{channel['id']}:{ts}"
    reactions = WORLD.state["reactions"].setdefault(key, {})
    users = reactions.setdefault(args["name"], [])
    if AUTHED_USER in users:
        raise ToolError("already_reacted")
    users.append(AUTHED_USER)
    return {"ok": True}


def reactions_get(args: dict[str, Any]) -> dict[str, Any]:
    # This function returns the reactions on a message.
    channel = _require_channel(args)
    ts = args.get("timestamp")
    if not ts:
        raise ToolError("timestamp is required")
    message = WORLD.message(channel["id"], ts)
    if message is None:
        raise ToolError(f"message_not_found: no message at {ts} in #{channel['name']}")

    reactions = WORLD.state["reactions"].get(f"{channel['id']}:{ts}", {})
    return {
        "ok": True,
        "type": "message",
        "channel": channel["id"],
        "message": {
            "ts": ts,
            "text": message["text"],
            "reactions": [{"name": n, "users": u, "count": len(u)} for n, u in reactions.items()],
        },
    }


def pins_add(args: dict[str, Any]) -> dict[str, Any]:
    # This function pins a message to a channel.
    channel = _require_channel(args)
    ts = args.get("timestamp")
    if not ts:
        raise ToolError("timestamp is required")
    if WORLD.message(channel["id"], ts) is None:
        raise ToolError(f"message_not_found: no message at {ts} in #{channel['name']}")

    pins = WORLD.state["pins"].setdefault(channel["id"], [])
    if ts in pins:
        raise ToolError("already_pinned")
    pins.append(ts)
    return {"ok": True}


def pins_remove(args: dict[str, Any]) -> dict[str, Any]:
    # This function unpins a message from a channel.
    channel = _require_channel(args)
    ts = args.get("timestamp")
    if not ts:
        raise ToolError("timestamp is required")

    pins = WORLD.state["pins"].setdefault(channel["id"], [])
    if ts not in pins:
        raise ToolError(f"no_pin: {ts} is not pinned in #{channel['name']}")
    pins.remove(ts)
    return {"ok": True}


HANDLERS = {
    "slack_auth_test": auth_test,
    "slack_chat_post_message": chat_post_message,
    "slack_conversations_history": conversations_history,
    "slack_conversations_info": conversations_info,
    "slack_users_profile_get": users_profile_get,
    "slack_stars_add": stars_add,
    "slack_stars_list": stars_list,
    "slack_reactions_add": reactions_add,
    "slack_reactions_get": reactions_get,
    "slack_pins_add": pins_add,
    "slack_pins_remove": pins_remove,
}


def build() -> Server:
    # This function builds the Slack server.
    return build_server("slack", HANDLERS)


if __name__ == "__main__":
    run_stdio(build())
