"""Adds the headline operations the original stratified sample missed.

The sampler spread its picks across strata and happened to skip list-pull-requests and
post-message, which the demo scenarios need. This pass takes those operations from the same
pinned spec commits, converts them with the same rules, and marks them source="real_additive"
so the real/synthetic metric splits stay honest.

Conversion helpers below are copied verbatim from the original generator so an added tool is
byte-identical in shape to one the sampler picked.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import sys
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from catalog.models import Parameter, Tool  # noqa: E402

# Same commits fetch_specs.py pinned, so these operations come from the same spec revisions
# as the 280 real tools already in the catalogue.
SPECS = {
    "github": {
        "repo": "github/rest-api-description",
        "sha": "3cef12e8a02d612ad032473d4fb87266f2befeae",
        "path": "descriptions/api.github.com/api.github.com.json",
    },
    "slack": {
        "repo": "slackapi/slack-api-specs",
        "sha": "bc08db49625630e3585bf2f1322128ea04f2a7f3",
        "path": "web-api/slack_web_openapi_v2.json",
    },
}

# Only what scenarios 1 and 2 need. Each entry is (service, operationId, expected tool name,
# why it is here). The expected name is asserted, not trusted.
WANTED = [
    ("github", "repos/list-for-authenticated-user", "github_repos_list_for_authenticated_user",
     "scenario 1 says 'my' pull requests, so the repo has to be discoverable"),
    ("github", "pulls/list", "github_pulls_list",
     "scenario 1's answer, and the first half of scenario 2"),
    # Slack's spec uses underscored operationIds, not the dotted API method names.
    ("slack", "chat_postMessage", "slack_chat_post_message",
     "scenario 2's second call, the one that changes Slack world state"),
    ("slack", "conversations_history", "slack_conversations_history",
     "reads the posted message back, which is how scenario 2 is proved"),
]

HTTP_METHODS = ("get", "post", "put", "patch", "delete")


def resolve_ref(doc: dict, node: Any, depth: int = 0) -> Any:
    # This function resolves a local $ref against the document it came from.
    if not isinstance(node, dict) or "$ref" not in node or depth > 8:
        return node
    ref = node["$ref"]
    if not ref.startswith("#/"):
        return node
    target: Any = doc
    for part in ref[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(target, dict) or part not in target:
            return node
        target = target[part]
    return resolve_ref(doc, target, depth + 1)


def clean_text(raw: str) -> str:
    # This function strips HTML and markdown noise out of spec prose.
    text = html.unescape(raw or "")
    text = re.sub(r"<br\s*/?>", " ", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\[!\w+\]", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = text.replace("`", "").replace("*", "").replace(">", " ")
    return re.sub(r"\s+", " ", text).strip()


def pick_description(op: dict) -> str:
    # This function picks the description: summary first, else the first sentence of description.
    summary = clean_text(op.get("summary") or "")
    if summary:
        return summary[: config.MAX_DESCRIPTION_CHARS].strip()
    body = clean_text(op.get("description") or "")
    if not body:
        return ""
    first = re.split(r"(?<=[.!?])\s+", body)[0]
    return first[: config.MAX_DESCRIPTION_CHARS].strip()


def normalise_name(service: str, operation_id: str) -> str:
    # This function normalises an operationId mechanically, preserving the spec author's wording.
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", operation_id)
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", s)
    s = re.sub(r"[^A-Za-z0-9]+", "_", s).lower().strip("_")
    s = re.sub(r"_+", "_", s)
    if not s.startswith(f"{service}_"):
        s = f"{service}_{s}"
    return s


def extract_params(doc: dict, op: dict) -> list[Parameter]:
    # This function pulls parameter names and types out of one operation, resolving local $refs.
    out: list[Parameter] = []
    seen: set[str] = set()

    def add(name: str, type_: str, required: bool) -> None:
        if name and name not in seen:
            seen.add(name)
            out.append(Parameter(name=name, type=type_ or "string", required=bool(required)))

    for raw in op.get("parameters") or []:
        p = resolve_ref(doc, raw)
        if not isinstance(p, dict) or p.get("in") == "cookie":
            continue
        schema = resolve_ref(doc, p.get("schema") or {})
        add(p.get("name"), schema.get("type") or p.get("type"), p.get("required"))
        if p.get("in") == "body" and isinstance(schema, dict):
            for prop, spec in (schema.get("properties") or {}).items():
                spec = resolve_ref(doc, spec)
                add(prop, spec.get("type") if isinstance(spec, dict) else "string",
                    prop in (schema.get("required") or []))

    body = resolve_ref(doc, op.get("requestBody") or {})
    for media in (body.get("content") or {}).values():
        schema = resolve_ref(doc, media.get("schema") or {})
        if not isinstance(schema, dict):
            continue
        for prop, spec in (schema.get("properties") or {}).items():
            spec = resolve_ref(doc, spec)
            add(prop, spec.get("type") if isinstance(spec, dict) else "string",
                prop in (schema.get("required") or []))
        break

    return out


def fetch_spec(service: str) -> tuple[dict, dict]:
    # This function downloads one pinned spec unless it is already on disk.
    entry = SPECS[service]
    config.SPECS_DIR.mkdir(parents=True, exist_ok=True)
    dest = config.SPECS_DIR / f"{service}__{entry['path'].rsplit('/', 1)[-1]}"
    if not dest.exists():
        url = f"https://raw.githubusercontent.com/{entry['repo']}/{entry['sha']}/{entry['path']}"
        print(f"fetching {url}", file=sys.stderr)
        with urllib.request.urlopen(url, timeout=300) as resp:
            body = resp.read()
        json.loads(body)
        dest.write_bytes(body)
    raw = dest.read_bytes()
    provenance = {"repo": entry["repo"], "commit": entry["sha"], "spec_path": entry["path"],
                  "sha256": hashlib.sha256(raw).hexdigest()}
    return json.loads(raw), provenance


def find_operation(doc: dict, operation_id: str) -> tuple[str, str, dict]:
    # This function locates one operation by its operationId, failing loudly if it is not unique.
    matches = [
        (path, method, op)
        for path, item in (doc.get("paths") or {}).items()
        for method, op in (resolve_ref(doc, item) or {}).items()
        if method in HTTP_METHODS and isinstance(op, dict) and op.get("operationId") == operation_id
    ]
    if len(matches) != 1:
        raise RuntimeError(f"operationId {operation_id!r} matched {len(matches)} operations, want 1")
    return matches[0]


def main() -> None:
    catalog_doc = json.loads(config.CATALOG_PATH.read_text())
    existing = catalog_doc["tools"]
    existing_names = {t["name"] for t in existing}
    before = len(existing)

    added: list[dict] = []
    provenance: dict[str, dict] = {}
    docs: dict[str, dict] = {}

    for service, operation_id, expected_name, _reason in WANTED:
        if service not in docs:
            docs[service], provenance[service] = fetch_spec(service)
        doc = docs[service]

        path, method, op = find_operation(doc, operation_id)
        name = normalise_name(service, operation_id)
        if name != expected_name:
            raise RuntimeError(f"{operation_id}: normalised to {name!r}, expected {expected_name!r}")
        if name in existing_names:
            raise RuntimeError(f"{name} is already in the catalogue; nothing to add")

        description = pick_description(op)
        if not description:
            raise RuntimeError(f"{operation_id} has no usable description")
        params = extract_params(doc, op)
        truncated = len(params) > config.MAX_PARAMETERS
        params = params[: config.MAX_PARAMETERS]

        tool = Tool(name=name, description=description, parameters=params, service=service,
                    source="real_additive", spec_path=path, http_method=method.upper())
        added.append(tool.model_dump())
        existing_names.add(name)
        print(f"  + {name:44s} {method.upper():5s} {len(params):2d} params"
              f"{' (truncated)' if truncated else ''}  :: {description[:48]}")

    tools = existing + added
    names = [t["name"] for t in tools]
    collisions = sorted({n for n in names if names.count(n) > 1})
    if collisions:
        raise RuntimeError(f"duplicate tool names after the additive pass: {collisions}")

    config.CATALOG_PATH.write_text(json.dumps({"tools": tools}, indent=2) + "\n")

    stats_path = config.CATALOG_PATH.parent / "build_stats.json"
    stats = json.loads(stats_path.read_text())
    stats["additive"] = {
        "reason": "the stratified sample skipped the list-pull-requests and post-message "
                  "operations the demo scenarios need",
        "specs": provenance,
        "tools": [
            {"name": expected, "service": service, "operation_id": op_id, "why": reason}
            for service, op_id, expected, reason in WANTED
        ],
    }
    counts = {"real": 0, "synthetic": 0, "real_additive": 0}
    for t in tools:
        counts[t["source"]] = counts.get(t["source"], 0) + 1
    stats["totals"] = {**counts, "total": len(tools)}
    stats_path.write_text(json.dumps(stats, indent=2) + "\n")

    print(f"\n{before} -> {len(tools)} tools ({len(added)} added)")
    print(f"by source: {counts}")


if __name__ == "__main__":
    main()
