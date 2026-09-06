// One field, four presets. A preset fills the field rather than submitting it.

import { useEffect, useRef } from "react";
import type { FormEvent, KeyboardEvent } from "react";

// Labelled by what each demonstrates, not by the query text.
export const PRESETS: { label: string; query: string }[] = [
  {
    label: "Single tool",
    query: "what are my open pull requests on acme/api-server?",
  },
  {
    label: "Cross-server chain",
    query: "Find the newest open PR on acme/api-server and post a one-line summary of it " +
           "to the eng channel in Slack.",
  },
  {
    label: "Ambiguous",
    query: "send them the file",
  },
  {
    label: "Wrong toolbox",
    query: "In our Slack workspace conversation about channels and emoji and pins: " +
           "which pull requests are open on acme/api-server?",
  },
];

export function Input({ value, onChange, onSubmit, running, started }: {
  value: string;
  onChange: (v: string) => void;
  onSubmit: () => void;
  running: boolean;
  started: boolean;
}) {
  const box = useRef<HTMLTextAreaElement>(null);

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (!running && value.trim()) onSubmit();
  };

  // Enter sends, shift+enter starts a new line, the way a chat box behaves.
  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      if (!running && value.trim()) onSubmit();
    }
  };

  // Grow with the text instead of scrolling a one-line box, up to a sensible ceiling.
  useEffect(() => {
    const el = box.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 200)}px`;
  }, [value]);

  return (
    <div className="input">
      <div className="col">
        <div className="presets">{!started && (<>
          {PRESETS.map((p) => (
            <button
              key={p.label}
              type="button"
              className="preset"
              disabled={running}
              onClick={() => onChange(p.query)}
            >
              {p.label}
            </button>
          ))}</>)}
          {started && <span className="hint">Follow-ups keep the same conversation.</span>}
        </div>
        <form onSubmit={submit}>
          <textarea
            ref={box}
            value={value}
            disabled={running}
            rows={1}
            placeholder={started ? "Ask a follow-up…" : "Ask something across 504 tools…"}
            onChange={(e) => onChange(e.target.value)}
            onKeyDown={onKey}
            autoFocus
          />
          <button type="submit" disabled={running || !value.trim()}>
            {running ? "Running" : "Run"}
          </button>
        </form>
      </div>
    </div>
  );
}
