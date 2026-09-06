// Servers and tool counts, always visible. A server going down changes a dot and the total.

import type { ServerStatus } from "../types";

export const TITLE = "Multi-Tool Agentic Orchestration";

export function Header({ servers, totalTools, onReset, busy }: {
  servers: ServerStatus[];
  totalTools: number;
  onReset: () => void;
  busy: boolean;
}) {
  const down = servers.filter((s) => !s.connected).length;

  return (
    <header className="header">
      <div className="col">
        <span className="brand">{TITLE}</span>
        <span className="counts">
          {servers.length} MCP servers
          {down > 0 && <span className="bad"> · {down} down</span>}
          <span className="sep">|</span>
          {totalTools} tools
        </span>
        <div className="dots">
          {servers.map((s) => (
            <span key={s.name} className="dot-hit" tabIndex={0}>
              <span className={`dot ${s.connected ? "up" : "down"}`} />
              <span className="tip" role="tooltip">
                <b>{s.name}</b>
                {s.connected
                  ? ` · ${s.tool_count} tools`
                  : ` · unavailable${s.error ? ` · ${s.error.slice(0, 70)}` : ""}`}
              </span>
            </span>
          ))}
        </div>
        <button className="preset reset" onClick={onReset} disabled={busy}
                title="Clear the conversation and restore the seed world">
          Reset
        </button>
      </div>
    </header>
  );
}

export function Sent({ sent, tokens, totalTools }: {
  sent: number | null; tokens: number | null; totalTools: number;
}) {
  if (sent === null) return null;
  const t = tokens === null ? null
    : tokens >= 1000 ? `${(tokens / 1000).toFixed(1)}k` : String(tokens);
  return (
    <div className="sent">
      <div className="col">
        <b>{sent}</b> of {totalTools} tools sent to the model
        {t && <><span className="sep">|</span>{t} tokens</>}
      </div>
    </div>
  );
}
