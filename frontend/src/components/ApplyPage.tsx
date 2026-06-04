import { useState, useRef } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { api } from "../api";
import type { LogEvent } from "../types";

export default function ApplyPage() {
  const qc = useQueryClient();
  const [lines, setLines] = useState<{ text: string; type: string }[]>([]);
  const [running, setRunning] = useState(false);
  const logRef = useRef<HTMLPreElement>(null);

  function appendLine(text: string, type = "log") {
    setLines((l) => [...l, { text, type }]);
    setTimeout(() => {
      if (logRef.current)
        logRef.current.scrollTop = logRef.current.scrollHeight;
    }, 0);
  }

  async function runApply(opts: object) {
    if (running) return;
    setRunning(true);
    setLines([]);
    try {
      await api.applyStream(opts, (evt: LogEvent) => {
        if (evt.type === "log") appendLine(evt.msg, "log");
        else if (evt.type === "done")
          appendLine(`✓ Done. Cost: $${evt.cost.toFixed(4)}`, "done");
        else if (evt.type === "error") appendLine(`✗ ${evt.msg}`, "err");
      });
      qc.invalidateQueries({ queryKey: ["state"] });
    } catch (e) {
      appendLine(`✗ ${e}`, "err");
    } finally {
      setRunning(false);
    }
  }

  async function runPrune() {
    if (!confirm("Remove state records and output files for assets deleted from the spec?"))
      return;
    try {
      const r = await api.prune();
      appendLine(`Pruned ${r.removed.length} orphan(s).`, "done");
      qc.invalidateQueries({ queryKey: ["state"] });
    } catch (e) {
      appendLine(`✗ ${e}`, "err");
    }
  }

  return (
    <div>
      <div className="section-head" style={{ marginTop: 0 }}>
        <h2>Apply</h2>
        <div className="section-actions">
          <button
            className="btn btn-primary"
            disabled={running}
            onClick={() => runApply({})}
          >
            {running ? "Running…" : "Apply"}
          </button>
          <button
            className="btn"
            disabled={running}
            onClick={() => runApply({ force: true })}
          >
            Force All
          </button>
          <button className="btn btn-danger" disabled={running} onClick={runPrune}>
            Prune Orphans
          </button>
        </div>
      </div>

      <div className="notice notice-info">
        <strong>Apply</strong> generates all missing or spec-drifted assets and updates
        state. <strong>Force All</strong> regenerates everything regardless of state.{" "}
        <strong>Prune</strong> deletes files for assets removed from the brandspec.
      </div>

      <pre className="log-box" ref={logRef}>
        {lines.length === 0 && (
          <span className="log-placeholder">
            Run Apply to stream live progress here…
          </span>
        )}
        {lines.map((l, i) => (
          <span key={i} className={`log-line log-${l.type}`}>
            {l.text + "\n"}
          </span>
        ))}
      </pre>
    </div>
  );
}
