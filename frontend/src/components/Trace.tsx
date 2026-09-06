// The centrepiece. One row per event, expandable where there is more to see.

import { useEffect, useRef, useState } from "react";
import { Markdown } from "../markdown";
import type { CallRow, Row } from "../types";

const MAX_INLINE = 78;

// Truncates in the middle, so the end of an argument or id stays readable.
function middleEllipsis(text: string, max = MAX_INLINE): string {
  if (text.length <= max) return text;
  const half = Math.floor((max - 1) / 2);
  return `${text.slice(0, half)}…${text.slice(-half)}`;
}

function args(a: Record<string, unknown>): string {
  const inner = Object.entries(a)
    .map(([k, v]) => `${k}: ${typeof v === "string" ? `"${v}"` : JSON.stringify(v)}`)
    .join(", ");
  return `{ ${inner} }`;
}

function tokens(n: number): string {
  return n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n);
}

// One line that expands to its full value on click.
function Expandable({ tick, text }: { tick: string; text: string }) {
  const [open, setOpen] = useState(false);
  const long = text.length > MAX_INLINE;
  return (
    <>
      <button
        className="sub clickable"
        onClick={() => long && setOpen(!open)}
        aria-expanded={long ? open : undefined}
      >
        <span className="tick">{tick}</span>
        <span className="val">{open ? "" : middleEllipsis(text)}</span>
      </button>
      {open && <div className="open">{text}</div>}
    </>
  );
}

function Collapsible({ summary, children, open: initial = false }:
  { summary: string; children: React.ReactNode; open?: boolean }) {
  const [open, setOpen] = useState(initial);
  return (
    <div className="disclosure">
      <button className="disclosure-toggle" onClick={() => setOpen(!open)} aria-expanded={open}>
        <span className="caret">{open ? "▾" : "▸"}</span>{summary}
      </button>
      {open && <div className="disclosure-body">{children}</div>}
    </div>
  );
}


function Call({ row, lead }: { row: CallRow; lead: boolean }) {
  return (
    <div className={`row${row.batch > 1 ? " batched" : ""}`}>
      {lead && row.batch > 1 && (
        <div className="batch-note">{row.batch} calls dispatched in parallel</div>
      )}
      <div className="line">
        <span className="server">{row.server}</span>
        <span className="tool">{row.tool}</span>
        {row.done && <span className="ms">{Math.round(row.ms ?? 0)} ms</span>}
      </div>
      {!row.done && <div className="sub"><span className="tick">└</span>
        <span className="val">running…</span></div>}
      {row.done && row.ok && (
        <Collapsible summary="arguments and result">
          <div className="open">{args(row.args)}</div>
          <div className="open">{row.summary ?? "ok"}</div>
        </Collapsible>
      )}
      {row.done && !row.ok && (
        // Failures never collapse: the retry path working is part of the demonstration.
        <>
          <Expandable tick="├" text={args(row.args)} />
          <div className="sub">
            <span className="tick">└</span>
            <span className="bad">
              {row.error}
              {(row.attempts ?? 1) > 1 && ` · ${row.attempts} attempts`}
            </span>
          </div>
        </>
      )}
    </div>
  );
}

function One({ row, lead }: { row: Row; lead: boolean }) {
  switch (row.kind) {
    case "ask":
      return (
        <div className="row ask">
          <div className="say">{row.text}</div>
        </div>
      );

    case "selected":
      return (
        <div className="row">
          <div className="line">
            <span style={{ color: "var(--accent)" }}>{row.k} tools retrieved</span>
            <span className="muted">
              {Object.entries(row.servers).map(([s, n]) => `${s} ${n}`).join("  ·  ")}
            </span>
            <span className="ms">{tokens(row.sent)} tokens</span>
          </div>
          <Collapsible summary={`${row.tools.length} tool names`}>
            <ul className="selected">
              {row.tools.map((t) => <li key={t.key}><span>{t.key}</span></li>)}
            </ul>
          </Collapsible>
        </div>
      );

    case "widened":
      return (
        <div className="row">
          <div className="line">
            <span style={{ color: "var(--accent)" }}>
              {row.refused ? "Wrong toolbox · widen refused" : "Wrong toolbox"}
            </span>
            <span className="muted">widened {row.from} → {row.to}</span>
          </div>
          {row.reason && <Expandable tick="└" text={row.reason} />}
        </div>
      );

    case "context-lost":
      return (
        <div className="row">
          <div className="line say bad">
            The server restarted, so earlier turns are no longer in context.
          </div>
        </div>
      );

    case "clarify":
      return (
        <div className="row">
          <div className="line say muted">Needs clarification · human in the loop</div>
          <div className="line say" style={{ paddingTop: "var(--s1)" }}>{row.question}</div>
        </div>
      );

    case "unavailable":
      return (
        <div className="row">
          <div className="line">
            <span className="bad">server unavailable</span>
            <span className="muted">{row.server}</span>
          </div>
          {row.error && <Expandable tick="└" text={row.error} />}
        </div>
      );

    case "error":
      return (
        <div className="row">
          <div className="line"><span className="bad">{row.stage}</span></div>
          <div className="sub"><span className="tick">└</span>
            <span className="bad">{row.message}</span></div>
        </div>
      );

    case "answer":
      return (
        <div className="row answer">
          <Markdown text={row.text} />
          <div className="line muted meta">
            <span>{row.turns} turn{row.turns === 1 ? "" : "s"}</span>
            {!row.routed && (
              <span>{row.loaded > 0
                ? `no retrieval needed · ${row.loaded} tools already loaded`
                : "answered from context · no tools"}</span>
            )}
            {row.capped && <span className="bad">iteration cap reached</span>}
            <span className="ms">{(row.ms / 1000).toFixed(1)}s</span>
          </div>
        </div>
      );

    case "call":
      return <Call row={row} lead={lead} />;
  }
}

export function Trace({ rows, running, status }:
  { rows: Row[]; running: boolean; status: string | null }) {
  const box = useRef<HTMLDivElement>(null);
  const stick = useRef(true);

  // Follow the newest row, but stop the moment the reader scrolls up to look at something.
  useEffect(() => {
    const el = box.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  }, [rows]);

  const onScroll = () => {
    const el = box.current;
    if (!el) return;
    stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
  };

  return (
    <div className="trace" ref={box} onScroll={onScroll}>
      <div className="col">
        {rows.length === 0 && !running && (
          <div className="empty">
            <p className="greeting">Hello. I'm here to help with your pull requests and code
               review, team chat, customer and support records, payments, calendars and files.</p>
            <p>Ask me anything below, or try one of the presets.</p>
          </div>
        )}
        {rows.map((row, i) => {
          const prev = rows[i - 1];
          const lead = row.kind !== "call" || prev?.kind !== "call" || prev.turn !== row.turn;
          return <One key={i} row={row} lead={lead} />;
        })}
        {status && (
          <div className="row status"><span className="pulse" />{status}</div>
        )}
      </div>
    </div>
  );
}
