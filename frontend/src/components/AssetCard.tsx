import type { AssetRecord } from "../types";

const STATUS_CLASS: Record<string, string> = {
  completed: "badge-ok",
  failed: "badge-bad",
  needs_attention: "badge-warn",
  pending: "badge-pending",
};

function assetUrl(rec: AssetRecord): string | null {
  if (!rec.output_path || rec.status !== "completed") return null;
  // Normalise Windows backslashes and strip leading path segments up to /out/
  const normalised = rec.output_path.replace(/\\/g, "/");
  const idx = normalised.indexOf("/out/");
  const rel = idx >= 0 ? normalised.slice(idx + 1) : normalised;
  return "/" + rel;
}

function isImage(rec: AssetRecord) {
  const p = rec.output_path ?? "";
  return p.endsWith(".jpg") || p.endsWith(".jpeg") || p.endsWith(".png");
}

interface Props {
  record: AssetRecord;
  onRegen: (id: string) => void;
  onDelete: (id: string) => void;
}

export default function AssetCard({ record: r, onRegen, onDelete }: Props) {
  const url = assetUrl(r);
  const img = url && isImage(r);
  const isContent = !img && url;

  return (
    <div className="card">
      {img && (
        <div className={`card-media ${r.group === "hero" ? "wide" : ""}`}>
          <img src={url} alt={r.label} loading="lazy" />
        </div>
      )}
      {isContent && <ContentPreview url={url} />}
      {!url && (
        <div className="card-placeholder">
          <span className="ph-sym">
            {r.status === "failed" ? "✗" : r.status === "needs_attention" ? "~" : "…"}
          </span>
        </div>
      )}

      <div className="card-body">
        <div className="card-title">
          {r.label || r.asset_id}
          <span className={`badge ${STATUS_CLASS[r.status] ?? "badge-pending"}`}>
            {r.status.replace(/_/g, " ")}
          </span>
        </div>
        <div className="card-meta">
          {r.asset_id} · {r.model}
          {r.input_tokens + r.output_tokens > 0 &&
            ` · ${(r.input_tokens + r.output_tokens).toLocaleString()} tok`}
          {r.cost > 0 && ` · $${r.cost.toFixed(5)}`}
        </div>
        {r.failure_reason && (
          <div className="card-error">{r.failure_reason}</div>
        )}
        <div className="card-actions">
          <button className="btn btn-sm" onClick={() => onRegen(r.asset_id)}>
            Regen
          </button>
          <button
            className="btn btn-sm btn-danger"
            onClick={() => onDelete(r.asset_id)}
          >
            Delete
          </button>
        </div>
      </div>
    </div>
  );
}

function ContentPreview({ url }: { url: string }) {
  const [text, setText] = React.useState<string>("Loading…");
  React.useEffect(() => {
    fetch(url)
      .then((r) => r.text())
      .then((t) => setText(t.replace(/<!--.*?-->/gs, "").trim()))
      .catch(() => setText("(could not load)"));
  }, [url]);
  return <div className="card-copy">{text}</div>;
}

import React from "react";
