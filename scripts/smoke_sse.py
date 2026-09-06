"""Step 5 proof: the three endpoints over HTTP, including the trace arriving as it happens."""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402

BASE = f"http://{config.HOST}:{config.PORT}"
failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not condition:
        failures.append(label)


def get(path: str) -> dict:
    with urllib.request.urlopen(f"{BASE}{path}", timeout=60) as r:
        return json.loads(r.read())


def post(path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(f"{BASE}{path}", method="POST",
                                 data=json.dumps(body or {}).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read())


def stream(query: str, max_seconds: float = 300) -> list[tuple[float, dict]]:
    # This function reads the SSE stream with curl, timestamping each event on arrival.
    proc = subprocess.Popen(
        ["curl", "-sN", "--max-time", str(int(max_seconds)), "-X", "POST", f"{BASE}/chat",
         "-H", "Content-Type: application/json", "-d", json.dumps({"query": query})],
        stdout=subprocess.PIPE, text=True,
    )
    started = time.monotonic()
    out: list[tuple[float, dict]] = []
    for line in proc.stdout:
        if line.startswith("data: "):
            out.append((time.monotonic() - started, json.loads(line[6:])))
    proc.wait()
    return out


def main() -> int:
    print("\n1. GET /servers")
    s = get("/servers")
    check("all 11 servers connected", s["connected"] == 11, f"{s['connected']}/{s['total']}")
    check("504 tools reported", s["total_tools"] == 504, str(s["total_tools"]))
    check("the index matches the pool", s["indexed_tools"] == s["total_tools"])
    check("router config is exposed", s["router"]["retriever"] == config.ROUTER_RETRIEVER,
          json.dumps(s["router"]))

    print("\n2. POST /chat streams the trace")
    events = stream("what are my open pull requests on acme/api-server?")
    types = [e["type"] for _, e in events]
    check("stream opened with server status", types[0] == "servers_connected")
    # Retrieval is on demand, so the model takes a turn to decide before routing happens.
    check("the model decided before any retrieval",
          types.index("turn_started") < types.index("tools_selected"))
    check("retrieval happened for a data question", "tools_selected" in types)
    check("a tool was called and finished", "tool_call_finished" in types)
    finished = [e for _, e in events if e["type"] == "tool_call_finished"]
    check("the call succeeded", all(e["data"]["ok"] for e in finished),
          str([e["data"]["error"] for e in finished if not e["data"]["ok"]]))
    check("stream ended with done", types[-1] == "done" and events[-1][1]["data"]["ok"])

    selected = next(e for _, e in events if e["type"] == "tools_selected")
    check("token saving is on the wire",
          selected["data"]["estimated_tokens_saved"] > 40000,
          f"~{selected['data']['estimated_tokens_saved']:,} saved")

    print("\n3. the trace streams live, it is not buffered to the end")
    # If the response were buffered, every event would land at the same instant.
    first_at = events[0][0]
    answer_at = next(t for t, e in events if e["type"] == "final_answer")
    check("the first event arrives well before the answer", answer_at - first_at > 1.0,
          f"first {first_at:.2f}s, answer {answer_at:.2f}s")
    check("events are spread out over the run",
          len({round(t, 1) for t, _ in events}) > 3,
          f"{len({round(t, 1) for t, _ in events})} distinct arrival times")

    print("\n4. POST /reset restarts every server")
    before = {s["name"]: s["connect_attempts"] for s in get("/servers")["servers"]}
    r = post("/reset")
    after = {s["name"]: s["connect_attempts"] for s in get("/servers")["servers"]}
    check("reset reports ok", r["ok"] and r["connected"] == 11)
    check("every server was restarted",
          all(after[n] == before[n] + 1 for n in before),
          f"{sum(after[n] > before[n] for n in before)}/11 restarted")
    check("tools are all back", get("/servers")["total_tools"] == 504)

    print("\n5. errors reach the client as events, never as a 500")
    events = stream("")
    types = [e["type"] for _, e in events]
    check("an empty query still returns a 200 stream", bool(events))
    check("it terminates cleanly", types[-1] == "done", types[-1] if types else "no events")

    print()
    if failures:
        print(f"FAILED {len(failures)}: " + "; ".join(failures))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
