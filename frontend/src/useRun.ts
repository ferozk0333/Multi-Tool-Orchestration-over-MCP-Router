// Runs one query and turns the SSE event stream into trace rows as they arrive.
// Rows accumulate across turns: a follow-up continues the conversation.

import { useCallback, useRef, useState } from "react";
import type { PipelineState, Stage } from "./components/Pipeline";
import type { Row, ServerStatus, TraceEvent } from "./types";

const API = "/api";
const MIN_TOOL_DWELL_MS = 450;

// EventSource cannot POST, so read the body stream and split on SSE frame boundaries.
async function* frames(res: Response): AsyncGenerator<TraceEvent> {
  const reader = res.body!.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let cut: number;
    while ((cut = buffer.indexOf("\n\n")) !== -1) {
      const frame = buffer.slice(0, cut);
      buffer = buffer.slice(cut + 2);
      const line = frame.split("\n").find((l) => l.startsWith("data: "));
      if (line) yield JSON.parse(line.slice(6));
    }
  }
}

export interface RunState {
  rows: Row[];
  servers: ServerStatus[];
  totalTools: number;
  sent: number | null;
  tokens: number | null;
  running: boolean;
  // A transient line at the foot of the trace, replaced as the run moves on.
  status: string | null;
  pipeline: PipelineState;
}

const IDLE_PIPELINE: PipelineState = {
  stage: null, done: [], retrieved: null, calls: 0,
};

const EMPTY: RunState = {
  rows: [], servers: [], totalTools: 0, sent: null, tokens: null,
  running: false, status: null, pipeline: IDLE_PIPELINE,
};

export function useRun() {
  const [state, setState] = useState<RunState>(EMPTY);
  const abort = useRef<AbortController | null>(null);
  const session = useRef<string | null>(null);

  const loadServers = useCallback(async () => {
    try {
      const r = await fetch(`${API}/servers`).then((x) => x.json());
      setState((s) => ({ ...s, servers: r.servers, totalTools: r.total_tools }));
    } catch {
      /* the header stays empty; a run will report the failure in the trace */
    }
  }, []);

  const reset = useCallback(async () => {
    abort.current?.abort();
    session.current = null;
    try { await fetch(`${API}/reset`, { method: "POST" }); } catch { /* shown on next run */ }
    setState((s) => ({ ...EMPTY, servers: s.servers, totalTools: s.totalTools }));
    loadServers();
  }, [loadServers]);

  const run = useCallback(async (query: string) => {
    abort.current?.abort();
    const controller = new AbortController();
    abort.current = controller;

    // Keep previous turns on screen. Only the per-run status is cleared.
    setState((s) => ({
      ...s, running: true, status: "Thinking", sent: null, tokens: null,
      rows: [...s.rows, { kind: "ask", text: query }],
      pipeline: { stage: "query", done: [], retrieved: null, calls: 0 },
    }));

    const push = (row: Row) =>
      setState((s) => ({ ...s, rows: [...s.rows, row] }));
    const say = (status: string | null) => setState((s) => ({ ...s, status }));

    // Mocked tool calls finish in ~10ms, which is below the threshold of noticing. Hold the
    // Call tools node lit briefly so the stage is visible rather than a flicker.
    const enteredTools = { at: 0 };
    const go = (stage: Stage, extra: Partial<PipelineState> = {}) => {
      const move = () => setState((st) => at(st, stage, extra));
      if (stage === "tools") { enteredTools.at = performance.now(); move(); return; }
      const held = enteredTools.at ? MIN_TOOL_DWELL_MS - (performance.now() - enteredTools.at) : 0;
      if (held > 0) { window.setTimeout(move, held); return; }
      move();
    };

    try {
      const res = await fetch(`${API}/chat`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query, session_id: session.current }),
        signal: controller.signal,
      });
      if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`);

      for await (const ev of frames(res)) {
        if (ev.type === "servers_connected" && ev.data.session_id) {
          session.current = ev.data.session_id;
        }
        apply(ev, push, say, setState, go);
      }
    } catch (err) {
      if ((err as Error).name !== "AbortError") {
        push({ kind: "error", stage: "transport", message: String(err) });
      }
    } finally {
      setState((s) => ({ ...s, running: false, status: null }));
    }
  }, []);

  return { ...state, run, reset, loadServers };
}

// Moves the pipeline to a stage, remembering the ones already passed.
function at(s: RunState, stage: Stage, extra: Partial<PipelineState> = {}): RunState {
  const done = s.pipeline.stage && s.pipeline.stage !== stage
    ? Array.from(new Set([...s.pipeline.done, s.pipeline.stage]))
    : s.pipeline.done;
  return { ...s, pipeline: { ...s.pipeline, ...extra, stage, done } };
}

// Folds one event into the trace. Tool results attach to the row their call opened.
function apply(
  ev: TraceEvent,
  push: (row: Row) => void,
  say: (status: string | null) => void,
  setState: React.Dispatch<React.SetStateAction<RunState>>,
  go: (stage: Stage, extra?: Partial<PipelineState>) => void,
) {
  const d = ev.data;
  switch (ev.type) {
    case "servers_connected":
      setState((s) => ({ ...s, servers: d.servers, totalTools: d.total_tools }));
      // The server did not have the conversation we thought we were continuing.
      if (d.resumed === false) push({ kind: "context-lost" });
      break;
    case "routing_started":
      say(`Retrieving tools for: ${d.need || d.query}`);
      go("retrieve");
      break;
    case "tools_selected":
      setState((s) => ({
        ...s, sent: d.k, tokens: d.estimated_tokens_selected,
        pipeline: { ...s.pipeline, retrieved: `${d.k} of ${s.totalTools}` },
      }));
      go("retrieve");
      push({
        kind: "selected", k: d.k, servers: d.servers, tools: d.tools,
        saved: d.estimated_tokens_saved, sent: d.estimated_tokens_selected,
        widened: d.widened,
      });
      say("Thinking");
      break;
    case "turn_started":
      say("Thinking");
      go("model");
      break;
    case "tools_widened":
      push({
        kind: "widened", from: d.old_k, to: d.new_k, reason: d.reason ?? "",
        refused: Boolean(d.refused),
      });
      break;
    case "tool_call_started":
      say(null);
      go("tools");
      setState((s) => ({ ...s, pipeline: { ...s.pipeline, calls: s.pipeline.calls + 1 } }));
      push({ kind: "call", id: d.id, server: d.server, tool: shortName(d.tool),
             args: d.arguments ?? {}, done: false,
             turn: d.turn ?? 0, batch: d.batch ?? 1 });
      break;
    case "tool_call_finished":
      setState((s) => ({
        ...s,
        status: "Thinking",
        rows: s.rows.map((r) =>
          r.kind === "call" && r.id === d.id
            ? { ...r, done: true, ok: d.ok, ms: d.duration_ms, attempts: d.attempts,
                summary: d.summary, error: d.error }
            : r,
        ),
      }));
      break;
    case "clarification_requested":
      say(null);
      push({ kind: "clarify", question: d.question });
      break;
    case "server_unavailable":
      push({ kind: "unavailable", server: d.server, error: d.error ?? "" });
      break;
    case "final_answer":
      say(null);
      go("answer");
      // A clarifying question already rendered as its own row.
      if (!d.clarification) {
        push({ kind: "answer", text: d.text, turns: d.turns, ms: d.duration_ms,
               capped: Boolean(d.capped), routed: d.routed !== false,
               loaded: d.tools_loaded ?? 0 });
      }
      break;
    case "error":
      push({ kind: "error", stage: d.stage ?? "run",
             message: d.error ?? d.note ?? "unknown error" });
      break;
    default:
      break;
  }
}

// Drops the server prefix, which the row already shows in its own column.
function shortName(qualified: string): string {
  const slash = qualified.indexOf("/");
  const name = slash === -1 ? qualified : qualified.slice(slash + 1);
  const underscore = name.indexOf("_");
  return underscore === -1 ? name : name.slice(underscore + 1);
}
