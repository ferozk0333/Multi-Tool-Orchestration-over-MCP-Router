"""All tunables. Plain constants, no YAML, no file I/O."""

from __future__ import annotations

import os
from pathlib import Path

SEED = 20260905

# --- paths ---
ROOT = Path(__file__).parent
CATALOG_PATH = ROOT / "catalog" / "catalog.json"
MCP_MANIFEST = ROOT / "mcp.json"     # server manifest, Claude Desktop shape
GOLDENS_PATH = ROOT / "evals" / "goldens.json"
CACHE_DIR = ROOT / ".cache"          # embeddings, keyed by hash of indexed text
SPECS_DIR = ROOT / "catalog" / "specs"   # downloaded OpenAPI specs, gitignored

# --- catalogue additive pass ---
# Same values the original generator used, so an added tool is shaped like its neighbours.
MAX_DESCRIPTION_CHARS = 200
MAX_PARAMETERS = 25

# --- models ---
# Needs native tool calling. Sonnet 5 by default: tool selection is not the hard part and the
# demo runs repeatedly. Override to compare, e.g. CHAT_MODEL=claude-opus-5.
CHAT_MODEL = os.environ.get("CHAT_MODEL") or "claude-sonnet-5"
MAX_RESPONSE_TOKENS = 8192           # headroom for adaptive thinking, which counts as output

# No TEMPERATURE. Sampling parameters were removed from the current models and from the SDK
# signature itself - anthropic 1.4.0 raises TypeError on temperature=. Verified, not assumed.
# Determinism comes from the seeded world and the router, not from sampling.

# Local, no API key. Anyone cloning the repo can reproduce embeddings.
EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"

# --- retrieval ---
# Dense, not hybrid. Measured on the goldens, fusing BM25 in cost 7 points of recall@12
# and won nothing: all three retrievers score 100% on lexical queries. See RETRIEVAL.md.
# bm25 and hybrid stay selectable so evals/report.py can keep reporting all three.
ROUTER_RETRIEVER = "dense"
ROUTER_K = 12                # tools shown to the model on a normal turn
WIDEN_K = 40                 # after request_more_tools
# A cross-server task can need capability from two services that the router missed, and the
# model discovers them one at a time - it asks for GitHub, uses it, then asks for Slack. At 1,
# the second request was refused and the task died half-finished. Widening only ever adds
# tools, and MAX_ITERATIONS still bounds the loop.
MAX_WIDENS = 2
# How many times one conversation may stop and ask the user a clarifying question before it
# has to proceed on best effort. Without a cap, an under-specified request can ping-pong.
MAX_CLARIFICATIONS = 3
RRF_K = 60
HYBRID_CANDIDATE_MULTIPLIER = 3
BM25_K1 = 1.5
BM25_B = 0.75

# --- agent loop ---
TOOL_MAX_RETRIES = 2         # ONE failing call. Conditional: transient only.
MAX_ITERATIONS = 10          # loop turns. Different problem, kept separate.
TOOL_CONCURRENCY = 5
TOOL_TIMEOUT_S = 10

# Errors worth retrying. Everything else fails fast - a deterministic error
# fails identically every time, so retrying only burns latency and noise.
RETRYABLE_EXCEPTIONS = ("TimeoutError", "ConnectionError", "TransientToolError")

# --- mcp servers ---
SERVERS = [
    "github", "stripe", "slack", "twilio", "crm", "ticketing",
    "calendar", "storage", "analytics", "hr", "inventory",
]
# Parameters the server supplies itself. In MCP the server holds its own credentials, so these
# are stripped from the advertised schema and injected before validation - the model never sees
# them. 32 catalogue tools declare `token` required; without this the model must invent a
# credential to call any of them, which is exactly what "never invent an identifier" forbids.
CREDENTIAL_PARAMS = {"token"}
SERVER_START_TIMEOUT_S = 15
SERVER_RECONNECT_INTERVAL_S = 30     # background retry for a server that died
TOOL_NAME_SEPARATOR = "/"            # namespaced as server/tool, names can collide across servers

# --- mock world ---
# Tools deliberately made unreliable so the retry and recovery paths actually execute.
# A branch that never runs in testing is untested, not working.
# Every name here is verified present in catalog.json - a mode keyed to a tool that
# does not exist is a path that silently never fires.
FLAKY_TOOLS = {
    "stripe_get_refunds": "timeout",        # times out on first attempt, succeeds on retry
    "slack_search_messages": "empty",       # returns zero rows, tests sibling-tool recovery
    "twilio_list_service_conversation_message": "validation",  # rejects args, must not be retried
}

# Set to a server name to make it fail on startup, so the pool's degraded path is
# demonstrable rather than theoretical. None in normal operation. Read from the environment
# because the servers are subprocesses: a test has to inject this across a process boundary.
FAIL_SERVER_ON_START: str | None = os.environ.get("FAIL_SERVER_ON_START") or None

# --- server ---
HOST = "127.0.0.1"
PORT = 8000
