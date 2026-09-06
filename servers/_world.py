"""Seeded in-memory world state. One slice per service, reset restores the seed."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

AUTHED_USER = "U01ALICE"

_SLACK_SEED: dict[str, Any] = {
    "team": {"id": "T01ACME", "name": "Acme", "domain": "acme"},
    "users": {
        "U01ALICE": {"id": "U01ALICE", "name": "alice", "real_name": "Alice Chen",
                     "title": "Staff Engineer", "tz": "America/Los_Angeles"},
        "U02BOB": {"id": "U02BOB", "name": "bob", "real_name": "Bob Ortiz",
                   "title": "Product Manager", "tz": "America/New_York"},
        "U03CAROL": {"id": "U03CAROL", "name": "carol", "real_name": "Carol Nwosu",
                     "title": "Engineering Manager", "tz": "Europe/London"},
    },
    "channels": {
        "C01ENG": {"id": "C01ENG", "name": "eng", "topic": "api-server work",
                   "members": ["U01ALICE", "U02BOB", "U03CAROL"], "is_private": False},
        "C02GENERAL": {"id": "C02GENERAL", "name": "general", "topic": "company-wide",
                       "members": ["U01ALICE", "U02BOB", "U03CAROL"], "is_private": False},
        "C03RANDOM": {"id": "C03RANDOM", "name": "random", "topic": "",
                      "members": ["U01ALICE", "U02BOB"], "is_private": False},
    },
    "messages": {
        "C01ENG": [
            {"ts": "1757030400.000100", "user": "U03CAROL", "text": "Standup in 10."},
            {"ts": "1757034000.000200", "user": "U01ALICE", "text": "Deploy of api-server is green."},
            {"ts": "1757037600.000300", "user": "U02BOB", "text": "Can someone review the auth PR?"},
        ],
        "C02GENERAL": [
            {"ts": "1757020000.000100", "user": "U02BOB", "text": "Welcome Carol to the team."},
        ],
        "C03RANDOM": [],
    },
    "stars": [
        {"type": "message", "channel": "C01ENG", "ts": "1757030400.000100", "user": AUTHED_USER},
    ],
    "pins": {"C01ENG": ["1757034000.000200"], "C02GENERAL": [], "C03RANDOM": []},
    "reactions": {
        "C01ENG:1757034000.000200": {"tada": ["U02BOB"]},
    },
}


_GITHUB_SEED: dict[str, Any] = {
    "owner": {"login": "acme", "type": "Organization"},
    "repos": {
        "api-server": {"id": 4101, "name": "api-server", "full_name": "acme/api-server",
                       "private": False, "default_branch": "main",
                       "description": "Public HTTP API", "language": "Python"},
        "web-client": {"id": 4102, "name": "web-client", "full_name": "acme/web-client",
                       "private": False, "default_branch": "main",
                       "description": "Customer-facing web app", "language": "TypeScript"},
        "infra": {"id": 4103, "name": "infra", "full_name": "acme/infra",
                  "private": True, "default_branch": "main",
                  "description": "Terraform and deploy tooling", "language": "HCL"},
    },
    # Newest last, so "the newest open PR" is a real ordering question rather than a coin flip.
    "pulls": {
        "api-server": [
            {"number": 476, "title": "Cache tenant lookups in the auth middleware",
             "state": "open", "user": "bob", "created_at": "2026-08-24T09:12:00Z",
             "base": "main", "head": "perf/auth-cache", "draft": False},
            {"number": 479, "title": "Drop the legacy /v1 billing routes",
             "state": "closed", "user": "carol", "created_at": "2026-08-27T16:40:00Z",
             "base": "main", "head": "chore/drop-v1-billing", "draft": False},
            {"number": 482, "title": "Add pagination to the events endpoint",
             "state": "open", "user": "alice", "created_at": "2026-09-01T11:05:00Z",
             "base": "main", "head": "feat/events-pagination", "draft": False},
        ],
        "web-client": [
            {"number": 118, "title": "Fix flaky checkout snapshot test",
             "state": "open", "user": "alice", "created_at": "2026-08-30T14:20:00Z",
             "base": "main", "head": "fix/checkout-snapshot", "draft": True},
        ],
        "infra": [],
    },
}


@dataclass
class GitHubWorld:
    state: dict[str, Any] = field(default_factory=lambda: copy.deepcopy(_GITHUB_SEED))

    def reset(self) -> None:
        # This method restores the seed world.
        self.state = copy.deepcopy(_GITHUB_SEED)

    def repo(self, owner: str, name: str) -> dict[str, Any] | None:
        # This method resolves a repo, accepting either "name" or "owner/name".
        if owner != self.state["owner"]["login"]:
            return None
        return self.state["repos"].get(name.split("/")[-1])

    def repo_names(self) -> list[str]:
        # This method lists full repo names, used to build correctable error messages.
        return sorted(r["full_name"] for r in self.state["repos"].values())


@dataclass
class SlackWorld:
    state: dict[str, Any] = field(default_factory=lambda: copy.deepcopy(_SLACK_SEED))

    def reset(self) -> None:
        # This method restores the seed world.
        self.state = copy.deepcopy(_SLACK_SEED)

    def channel_by_ref(self, ref: str) -> dict[str, Any] | None:
        # This method resolves a channel by id or by #name, the way Slack callers write it.
        ref = ref.lstrip("#")
        for channel in self.state["channels"].values():
            if ref in (channel["id"], channel["name"]):
                return channel
        return None

    def message(self, channel_id: str, ts: str) -> dict[str, Any] | None:
        # This method finds one message in a channel by timestamp.
        return next((m for m in self.state["messages"].get(channel_id, []) if m["ts"] == ts), None)

    def channel_names(self) -> list[str]:
        # This method lists the channel names, used to build correctable error messages.
        return sorted(c["name"] for c in self.state["channels"].values())
