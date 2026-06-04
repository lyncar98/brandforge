import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../api";
import AssetCard from "./AssetCard";
import type { AssetRecord } from "../types";

export default function AssetsPage() {
  const qc = useQueryClient();
  const { data, isLoading, error } = useQuery({
    queryKey: ["state"],
    queryFn: api.getState,
    refetchInterval: 10_000,
  });

  const deleteMut = useMutation({
    mutationFn: api.deleteAsset,
    onSuccess: () => qc.invalidateQueries({ queryKey: ["state"] }),
  });

  const regenMut = useMutation({
    mutationFn: async (id: string) => {
      // Trigger via apply with force + select, result will stream — for regen
      // we use a quick fire-and-forget POST then poll state.
      await fetch("/api/apply", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ select: [id], force: true }),
      });
    },
    onSuccess: () => {
      // Poll until done
      const interval = setInterval(() => {
        api.status().then((s) => {
          if (!s.busy) {
            clearInterval(interval);
            qc.invalidateQueries({ queryKey: ["state"] });
          }
        });
      }, 2000);
    },
  });

  if (isLoading) return <div className="page-msg">Loading assets…</div>;
  if (error) return <div className="page-msg error">{String(error)}</div>;

  const records = data?.records ?? [];
  if (!records.length)
    return (
      <div className="page-msg">
        No assets yet.{" "}
        <a href="#apply" className="link">
          Run Apply
        </a>{" "}
        to generate the full kit.
      </div>
    );

  const groups: Record<string, AssetRecord[]> = {};
  records.forEach((r) => {
    (groups[r.group] ??= []).push(r);
  });

  const totalCost = data?.cost ?? 0;

  return (
    <div>
      <div className="stat-row">
        <div className="stat">
          <div className="stat-k">Total assets</div>
          <div className="stat-v">{records.length}</div>
        </div>
        <div className="stat">
          <div className="stat-k">Completed</div>
          <div className="stat-v" style={{ color: "var(--good)" }}>
            {data?.counts?.completed ?? 0}
          </div>
        </div>
        <div className="stat">
          <div className="stat-k">Failed</div>
          <div className="stat-v" style={{ color: "var(--bad)" }}>
            {(data?.counts?.failed ?? 0) + (data?.counts?.needs_attention ?? 0)}
          </div>
        </div>
        <div className="stat">
          <div className="stat-k">Total cost</div>
          <div className="stat-v">${totalCost.toFixed(4)}</div>
        </div>
      </div>

      {Object.entries(groups).map(([group, recs]) => (
        <section key={group} className="group-section">
          <div className="section-head">
            <h2>{group}</h2>
            <span className="section-count">{recs.length} assets</span>
          </div>
          <div className="asset-grid">
            {recs.map((r) => (
              <AssetCard
                key={r.key}
                record={r}
                onRegen={(id) => regenMut.mutate(id)}
                onDelete={(id) => {
                  if (confirm(`Delete ${id} from state?`)) deleteMut.mutate(id);
                }}
              />
            ))}
          </div>
        </section>
      ))}
    </div>
  );
}
