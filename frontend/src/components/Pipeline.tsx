// The shape of a run, with the current stage lit. Five nodes and one loop.

export type Stage = "query" | "model" | "retrieve" | "tools" | "answer";

const STAGES: { id: Stage; label: string }[] = [
  { id: "query", label: "Query" },
  { id: "model", label: "Model" },
  { id: "retrieve", label: "Retrieve" },
  { id: "tools", label: "Call tools" },
  { id: "answer", label: "Answer" },
];

export interface PipelineState {
  stage: Stage | null;
  done: Stage[];
  retrieved: string | null;   // "12 of 504"
  calls: number;
}

export function Pipeline({ state }: { state: PipelineState }) {
  const note: Partial<Record<Stage, string>> = {
    retrieve: state.retrieved ?? "on demand",
    tools: state.calls ? `${state.calls} call${state.calls === 1 ? "" : "s"}` : "",
  };

  return (
    <div className="pipeline">
      <div className="col">
        <div className="flow">
          {STAGES.map((s, i) => (
            <div className="node-wrap" key={s.id}>
              {i > 0 && <span className="edge" aria-hidden="true" />}
              <div
                className={[
                  "node",
                  state.stage === s.id ? "active" : "",
                  state.done.includes(s.id) ? "visited" : "",
                ].join(" ").trim()}
              >
                <span className="node-label">{s.label}</span>
                {note[s.id] && <span className="node-note">{note[s.id]}</span>}
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
