const BASE = import.meta.env.VITE_API_URL ?? "";

async function req<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(BASE + path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(text || `HTTP ${res.status}`);
  }
  const ct = res.headers.get("content-type") ?? "";
  if (ct.includes("json")) return res.json() as Promise<T>;
  return res.text() as unknown as Promise<T>;
}

export const api = {
  getState: () => req<import("./types").StateResponse>("GET", "/api/state"),
  getSpec: () => req<Record<string, unknown>>("GET", "/api/spec"),
  putSpec: (content: unknown) => req<{ ok: boolean }>("PUT", "/api/spec", { content }),
  plan: (opts?: object) => req<import("./types").PlanAction[]>("POST", "/api/plan", opts ?? {}),
  prune: () => req<{ removed: { asset_id: string; group: string }[] }>("POST", "/api/prune", {}),
  deleteAsset: (id: string) => req<{ ok: boolean }>("DELETE", `/api/assets/${id}`),
  status: () => req<{ busy: boolean }>("GET", "/api/status"),

  applyStream(
    opts: object,
    onEvent: (e: import("./types").LogEvent) => void,
  ): Promise<void> {
    return new Promise((resolve, reject) => {
      fetch(BASE + "/api/apply", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(opts),
      })
        .then((res) => {
          const reader = res.body!.getReader();
          const dec = new TextDecoder();
          let buf = "";
          function pump() {
            reader.read().then(({ done, value }) => {
              if (done) { resolve(); return; }
              buf += dec.decode(value, { stream: true });
              const lines = buf.split("\n");
              buf = lines.pop() ?? "";
              for (const line of lines) {
                if (!line.startsWith("data:")) continue;
                try {
                  const evt = JSON.parse(line.slice(5).trim());
                  onEvent(evt);
                  if (evt.type === "done" || evt.type === "error") { resolve(); return; }
                } catch { /* skip malformed */ }
              }
              pump();
            });
          }
          pump();
        })
        .catch(reject);
    });
  },
};
