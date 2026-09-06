// Three regions, no navigation, no routing.

import { useEffect, useState } from "react";
import { Header, Sent, TITLE } from "./components/Header";
import { Input } from "./components/Input";
import { Pipeline } from "./components/Pipeline";
import { Trace } from "./components/Trace";
import { useRun } from "./useRun";

export default function App() {
  const run_ = useRun();
  const { rows, servers, totalTools, sent, tokens, running, status, pipeline,
          run, reset, loadServers } = run_;
  const [query, setQuery] = useState("");

  useEffect(() => { document.title = TITLE; }, []);
  useEffect(() => { loadServers(); }, [loadServers]);

  // ?q=... runs on load, so a demo link opens straight into a trace.
  useEffect(() => {
    const q = new URLSearchParams(window.location.search).get("q");
    if (q) { setQuery(q); submit(q); }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Sends the question and empties the field, the way a chat input behaves.
  function submit(q: string) {
    run(q);
    setQuery("");
  }

  return (
    <div className="app">
      <div>
        <Header servers={servers} totalTools={totalTools} onReset={reset} busy={running} />
        <Sent sent={sent} tokens={tokens} totalTools={totalTools} />
        <Pipeline state={pipeline} />
      </div>
      <Trace rows={rows} running={running} status={status} />
      <Input value={query} onChange={setQuery} onSubmit={() => submit(query)}
             running={running} started={rows.length > 0} />
    </div>
  );
}
