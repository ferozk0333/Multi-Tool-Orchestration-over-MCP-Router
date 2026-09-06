// The event schema the backend streams, plus the row shape the trace renders.

export type EventType =
  | "servers_connected" | "routing_started" | "tools_selected"
  | "turn_started" | "tool_call_started" | "tool_call_finished"
  | "tools_widened" | "clarification_requested"
  | "server_unavailable" | "final_answer" | "error" | "done";

export interface TraceEvent {
  type: EventType;
  ts: number;
  data: Record<string, any>;
}

export interface ServerStatus {
  name: string;
  connected: boolean;
  tool_count: number;
  error: string | null;
  connect_attempts: number;
}

export interface SelectedTool {
  key: string;
  score: number;
  rank: number;
}

// One tool call, with its result folded in once it finishes.
export interface CallRow {
  kind: "call";
  id: string;
  turn: number;
  batch: number;   // how many calls the model issued together this turn
  server: string;
  tool: string;
  args: Record<string, any>;
  done: boolean;
  ok?: boolean;
  ms?: number;
  attempts?: number;
  summary?: string | null;
  error?: string | null;
}

export type Row =
  | { kind: "ask"; text: string }
  | { kind: "context-lost" }
  | { kind: "selected"; k: number; servers: Record<string, number>;
      tools: SelectedTool[]; saved: number; sent: number; widened: boolean }
  | { kind: "widened"; from: number; to: number; reason: string; refused: boolean }
  | { kind: "clarify"; question: string }
  | { kind: "unavailable"; server: string; error: string }
  | { kind: "error"; stage: string; message: string }
  | { kind: "answer"; text: string; turns: number; ms: number; capped: boolean;
      routed: boolean; loaded: number }
  | CallRow;
