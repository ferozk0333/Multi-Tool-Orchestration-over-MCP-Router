"""GitHub MCP server: its catalogue slice, three tools wired to real repo and PR state."""

from __future__ import annotations

from typing import Any

from mcp.server.lowlevel import Server

from servers._base import ToolError, build_server, run_stdio
from servers._world import GitHubWorld

WORLD = GitHubWorld()


def _require_repo(args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    # This function resolves owner/repo or raises a message the model can act on.
    owner, name = args["owner"], args["repo"]
    repo = WORLD.repo(owner, name)
    if repo is None:
        raise ToolError(
            f"repo_not_found: {owner}/{name}. known repositories: {', '.join(WORLD.repo_names())}"
        )
    return repo["name"], repo


def repos_list_for_authenticated_user(args: dict[str, Any]) -> dict[str, Any]:
    # This function lists the repos the token can see, which is how a repo name gets discovered.
    repos = list(WORLD.state["repos"].values())
    visibility = args.get("visibility")
    if visibility == "public":
        repos = [r for r in repos if not r["private"]]
    elif visibility == "private":
        repos = [r for r in repos if r["private"]]
    return {"ok": True, "count": len(repos), "repositories": repos}


def pulls_list(args: dict[str, Any]) -> dict[str, Any]:
    # This function lists a repo's pull requests, filtered by state and sorted newest first.
    name, _repo = _require_repo(args)
    state = args.get("state", "open")
    if state not in ("open", "closed", "all"):
        raise ToolError(f"invalid state {state!r}: expected one of open, closed, all")

    pulls = WORLD.state["pulls"].get(name, [])
    if state != "all":
        pulls = [p for p in pulls if p["state"] == state]

    reverse = args.get("direction", "desc") != "asc"
    pulls = sorted(pulls, key=lambda p: p["created_at"], reverse=reverse)
    return {"ok": True, "count": len(pulls), "pull_requests": pulls}


def pulls_merge(args: dict[str, Any]) -> dict[str, Any]:
    # This function merges a pull request and persists the state change.
    name, _repo = _require_repo(args)
    number = args["pull_number"]
    pulls = WORLD.state["pulls"].get(name, [])
    pull = next((p for p in pulls if p["number"] == number), None)
    if pull is None:
        open_numbers = ", ".join(str(p["number"]) for p in pulls if p["state"] == "open")
        raise ToolError(f"pull_not_found: #{number} in {name}. open pull requests: {open_numbers}")
    if pull["state"] != "open":
        raise ToolError(f"pull_not_mergeable: #{number} is {pull['state']}")

    pull["state"] = "closed"
    pull["merged"] = True
    return {"ok": True, "merged": True, "message": f"Pull Request successfully merged: #{number}"}


HANDLERS = {
    "github_repos_list_for_authenticated_user": repos_list_for_authenticated_user,
    "github_pulls_list": pulls_list,
    "github_pulls_merge": pulls_merge,
}


def build() -> Server:
    # This function builds the GitHub server.
    return build_server("github", HANDLERS)


if __name__ == "__main__":
    run_stdio(build())
