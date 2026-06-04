import { useQuery } from "@tanstack/react-query";
import { api } from "../api";
import type { PlanAction } from "../types";

const ACTION_CLASS: Record<string, string> = {
  create: "sym-create",
  update: "sym-update",
  skip: "sym-skip",
  destroy: "sym-destroy",
};

const ACTION_BADGE: Record<string, string> = {
  create: "badge-create",
  update: "badge-update",
  skip: "badge-skip",
  destroy: "badge-bad",
};

export default function PlanPage() {
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["plan"],
    queryFn: () => api.plan({}),
    staleTime: 0,
  });

  const summary = (data ?? []).reduce(
    (acc, a) => ({ ...acc, [a.action]: (acc[a.action as keyof typeof acc] ?? 0) + 1 }),
    { create: 0, update: 0, skip: 0, destroy: 0 },
  );

  return (
    <div>
      <div className="section-head" style={{ marginTop: 0 }}>
        <h2>Plan</h2>
        {data && (
          <code className="plan-summary">
            +{summary.create} create &nbsp; ~{summary.update} update &nbsp; .
            {summary.skip} unchanged &nbsp; -{summary.destroy} destroy
          </code>
        )}
        <button className="btn" style={{ marginLeft: "auto" }} onClick={() => refetch()}>
          Refresh
        </button>
      </div>

      {isLoading && <div className="page-msg">Diffing spec against state…</div>}
      {error && <div className="page-msg error">{String(error)}</div>}

      {data && data.length === 0 && (
        <div className="page-msg">Everything is up to date.</div>
      )}

      {data && data.length > 0 && (
        <div className="plan-table">
          {data.map((a: PlanAction) => (
            <div key={a.asset_id} className="plan-row">
              <span className={`plan-sym ${ACTION_CLASS[a.action] ?? ""}`}>
                {a.symbol}
              </span>
              <span className={`badge ${ACTION_BADGE[a.action] ?? "badge-pending"}`}>
                {a.action}
              </span>
              <span className="plan-id">{a.asset_id}</span>
              <span className="plan-label">{a.label}</span>
              <span className="plan-detail">
                {a.model} · {a.detail}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
