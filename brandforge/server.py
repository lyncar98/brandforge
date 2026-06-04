"""BrandForge web UI - full-CRUD FastAPI server.

Exposes every CLI capability (plan / apply / prune / gallery / status) over a
REST + SSE API and serves a single-page React-like app that lets you browse
assets, edit the brandspec, run plan/apply with live streaming progress, and
manage state.

Start:
    python -m brandforge serve [--spec brands/lautum/brandspec.json] [--out out] [--port 7860]
"""
from __future__ import annotations

import json
import os
import queue
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .gallery import render_gallery
from .kit import BrandKit, KitConfig, KitRunner, load_kit
from .manifest import Manifest, job_key


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app(spec_path: str, out_dir: str) -> FastAPI:
    app = FastAPI(title="BrandForge", version="0.2.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
    )
    st = _AppState(spec_path=spec_path, out_dir=out_dir)
    _register_routes(app, st)
    return app


# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------

class _AppState:
    def __init__(self, spec_path: str, out_dir: str) -> None:
        self.spec_path = Path(spec_path)
        self.out_dir = Path(out_dir)
        self._run_thread: Optional[threading.Thread] = None

    @property
    def manifest_path(self) -> Path:
        return self.out_dir / "manifest.json"

    def load_kit(self) -> BrandKit:
        return load_kit(self.spec_path)

    def load_manifest(self) -> Optional[Manifest]:
        if self.manifest_path.exists():
            return Manifest.load(self.manifest_path)
        return None

    def is_busy(self) -> bool:
        return self._run_thread is not None and self._run_thread.is_alive()

    def _make_runner(self, kit: BrandKit, log=None) -> KitRunner:
        from .client import LumaAgentsClient
        from .content import ClaudeContentClient, MockContentClient
        from .mock import MockTransport, MockConfig

        luma_key = os.getenv("LUMA_AGENTS_API_KEY", "")
        ant_key = os.getenv("ANTHROPIC_API_KEY", "")
        mock = not luma_key or not ant_key
        if mock:
            ic = LumaAgentsClient("mock", transport=MockTransport(MockConfig()))
            cc = MockContentClient()
        else:
            ic = LumaAgentsClient(luma_key)
            cc = ClaudeContentClient(ant_key)
        config = KitConfig(output_dir=str(self.out_dir))
        return KitRunner(ic, cc, kit, config, log=log or (lambda m: None))


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def _register_routes(app: FastAPI, st: _AppState) -> None:

    # Spec
    @app.get("/api/spec")
    def get_spec():
        if not st.spec_path.exists():
            raise HTTPException(404, "Spec file not found")
        return JSONResponse(json.loads(st.spec_path.read_text("utf-8")))

    class SpecBody(BaseModel):
        content: Dict[str, Any]

    @app.put("/api/spec")
    def put_spec(body: SpecBody):
        try:
            BrandKit.from_dict(body.content)
        except Exception as exc:
            raise HTTPException(422, str(exc))
        st.spec_path.write_text(
            json.dumps(body.content, indent=2, ensure_ascii=False), "utf-8"
        )
        return {"ok": True}

    # State
    @app.get("/api/state")
    def get_state():
        m = st.load_manifest()
        if m is None:
            return {"records": [], "cost": 0.0, "counts": {}}
        records = [
            {
                "key": r.key,
                "asset_id": r.key.split("::")[-1],
                "group": r.product_id,
                "label": r.channel,
                "status": r.status,
                "model": r.model,
                "output_path": r.output_path,
                "failure_code": r.failure_code,
                "failure_reason": r.failure_reason,
                "input_tokens": r.input_tokens,
                "output_tokens": r.output_tokens,
                "cost": r.estimated_cost(),
                "updated_at": r.updated_at,
            }
            for r in m.records.values()
        ]
        return {"records": records, "cost": m.estimated_cost(), "counts": m.counts()}

    # Plan
    @app.post("/api/plan")
    def post_plan(body: Optional[Dict[str, Any]] = None):
        kit = st.load_kit()
        m = st.load_manifest()
        runner = st._make_runner(kit)
        select = _resolve_select(kit, body)
        actions = runner.plan(m, select=select)
        return [
            {
                "asset_id": a.asset_id, "kind": a.kind, "group": a.group,
                "label": a.label, "action": a.action, "model": a.model,
                "detail": a.detail, "symbol": a.symbol,
            }
            for a in actions
        ]

    # Apply (SSE)
    @app.post("/api/apply")
    def post_apply(body: Optional[Dict[str, Any]] = None):
        if st.is_busy():
            raise HTTPException(409, "An apply is already running")
        select = _resolve_select(st.load_kit(), body)
        force = bool((body or {}).get("force", False))
        log_q: queue.Queue = queue.Queue()

        def _run() -> None:
            try:
                kit = st.load_kit()
                manifest = st.load_manifest()

                def _log(msg: str) -> None:
                    log_q.put({"type": "log", "msg": msg})

                runner = st._make_runner(kit, log=_log)
                runner.config.force = force
                manifest = runner.run(manifest=manifest, select=select)
                render_gallery(kit, manifest, st.out_dir)
                log_q.put({
                    "type": "done",
                    "cost": manifest.estimated_cost(),
                    "counts": manifest.counts(),
                })
            except Exception as exc:
                log_q.put({"type": "error", "msg": str(exc)})

        st._run_thread = threading.Thread(target=_run, daemon=True)
        st._run_thread.start()

        def _stream():
            while True:
                try:
                    item = log_q.get(timeout=90)
                    yield f"data: {json.dumps(item)}\n\n"
                    if item["type"] in ("done", "error"):
                        break
                except queue.Empty:
                    yield 'data: {"type":"heartbeat"}\n\n'

        return StreamingResponse(
            _stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # Status
    @app.get("/api/status")
    def get_status():
        return {"busy": st.is_busy()}

    # Prune
    @app.post("/api/prune")
    def post_prune():
        if st.is_busy():
            raise HTTPException(409, "Apply is running")
        m = st.load_manifest()
        if m is None:
            return {"removed": []}
        kit = st.load_kit()
        runner = st._make_runner(kit)
        removed = runner.prune(m)
        m.save(st.manifest_path)
        if removed:
            render_gallery(kit, m, st.out_dir)
        return {"removed": [{"asset_id": a.asset_id, "group": a.group} for a in removed]}

    # Delete one asset
    @app.delete("/api/assets/{asset_id}")
    def delete_asset(asset_id: str):
        if st.is_busy():
            raise HTTPException(409, "Apply is running")
        m = st.load_manifest()
        if m is None:
            raise HTTPException(404, "No state")
        kit = st.load_kit()
        key = job_key(kit.brand_name, asset_id)
        rec = m.remove(key)
        if rec is None:
            raise HTTPException(404, f"Asset '{asset_id}' not in state")
        if rec.output_path:
            try:
                Path(rec.output_path).unlink()
            except OSError:
                pass
        m.save(st.manifest_path)
        render_gallery(kit, m, st.out_dir)
        return {"ok": True, "asset_id": asset_id}

    # Gallery HTML
    @app.get("/api/gallery")
    def get_gallery():
        gp = st.out_dir / "gallery.html"
        if not gp.exists():
            m = st.load_manifest()
            if m is None:
                raise HTTPException(404, "No state; run apply first")
            kit = st.load_kit()
            render_gallery(kit, m, st.out_dir)
        return FileResponse(gp, media_type="text/html")

    # Serve generated assets
    st.out_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/out", StaticFiles(directory=str(st.out_dir)), name="out")

    # SPA shell (catch-all must be last)
    @app.get("/", response_class=HTMLResponse)
    @app.get("/{catchall:path}", response_class=HTMLResponse)
    def spa(catchall: str = ""):
        return HTMLResponse(_SPA_HTML)


# ---------------------------------------------------------------------------
# Selection helper
# ---------------------------------------------------------------------------

def _resolve_select(kit: BrandKit, body: Optional[Dict]) -> Optional[set]:
    if not body:
        return None
    ids = body.get("select")
    group = body.get("group")
    only = body.get("only")
    if not ids and not group and not only:
        return None
    return {
        a.id for a in kit.assets
        if (not ids or a.id in ids)
        and (not group or a.group == group)
        and (not only or a.kind == only)
    }


_SPA_HTML = '''
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>BrandForge</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Archivo:wght@400;600;700;800&family=Manrope:wght@400;500;600;700&family=Spline+Sans+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>*{box-sizing:border-box;margin:0;padding:0}body{background:#000;color:#f5f5f5;font-family:\'Manrope\',system-ui,sans-serif;-webkit-font-smoothing:antialiased;line-height:1.5}:root{--ink:#000;--panel:#0a0a0a;--panel2:#111;--line:#242424;--line2:#333;--mut:#888;--txt:#f5f5f5;--dim:#bbb;--str:#fff;--acc:#fff;--good:#5bbf8a;--warn:#d6a44a;--bad:#df6b5c;--disp:\'Archivo\',\'Manrope\',sans-serif;--mono:\'Spline Sans Mono\',monospace}a{color:inherit;text-decoration:none}.nav{position:sticky;top:0;z-index:20;display:flex;align-items:center;gap:12px;padding:16px 32px;border-bottom:1px solid var(--line);background:rgba(0,0,0,.88);backdrop-filter:blur(14px)}.brand{font-family:var(--disp);font-weight:800;font-size:18px;letter-spacing:-.3px}.brand .dot{display:inline-block;width:9px;height:9px;border:1.5px solid #fff;border-radius:2px;transform:rotate(45deg);margin-right:8px}.nav .tabs{display:flex;gap:2px;margin-left:24px}.tab{padding:6px 14px;border-radius:8px;font-size:13px;font-weight:500;color:var(--mut);cursor:pointer;border:none;background:none;font-family:inherit}.tab:hover{color:var(--txt);background:var(--panel)}.tab.active{color:var(--str);background:var(--panel2);border:1px solid var(--line)}.nav .right{margin-left:auto;display:flex;gap:8px;align-items:center}.btn{padding:7px 16px;border-radius:8px;font-size:13px;font-weight:600;cursor:pointer;border:1px solid var(--line);background:var(--panel);color:var(--txt);font-family:inherit;transition:all .15s}.btn:hover{background:var(--panel2);border-color:var(--line2)}.btn.primary{background:#fff;color:#000;border-color:#fff}.btn.primary:hover{background:#e8e8e8}.btn.danger{border-color:rgba(223,107,92,.5);color:var(--bad)}.btn:disabled{opacity:.4;cursor:not-allowed}.page{display:none;max-width:1200px;margin:0 auto;padding:32px 32px 80px}.page.active{display:block}.section-head{display:flex;align-items:center;gap:10px;margin-bottom:20px;padding-bottom:14px;border-bottom:1px solid var(--line)}.section-head h2{font-family:var(--disp);font-weight:700;font-size:16px}.section-head .sub{font-size:12px;color:var(--mut);margin-left:4px;font-family:var(--mono,monospace)}.section-head .ml{margin-left:auto}.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:14px}.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;overflow:hidden;transition:border-color .15s,transform .15s}.card:hover{border-color:var(--line2);transform:translateY(-1px)}.card .media{aspect-ratio:1;background:#0a0a0a;display:flex;align-items:center;justify-content:center;border-bottom:1px solid var(--line)}.card .media img{width:100%;height:100%;object-fit:cover}.card .media.wide{aspect-ratio:16/9}.card .body{padding:14px}.card .title{font-weight:600;font-size:14px;margin-bottom:4px}.card .meta{font-size:11px;color:var(--mut);font-family:var(--mono,monospace)}.card .actions{display:flex;gap:6px;margin-top:10px}.badge{display:inline-flex;align-items:center;padding:2px 8px;border-radius:5px;font-size:10px;font-family:var(--mono,monospace);border:1px solid}.badge.ok{color:var(--good);border-color:rgba(91,191,138,.35)}.badge.warn{color:var(--warn);border-color:rgba(214,164,74,.35)}.badge.bad{color:var(--bad);border-color:rgba(223,107,92,.4)}.badge.pending{color:var(--mut);border-color:var(--line2)}.badge.create{color:#7ec8e3;border-color:rgba(126,200,227,.35)}.badge.update{color:var(--warn);border-color:rgba(214,164,74,.35)}.badge.skip{color:var(--mut);border-color:var(--line)}.badge.destroy{color:var(--bad);border-color:rgba(223,107,92,.4)}.log-box{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px;font-family:var(--mono,monospace);font-size:12px;color:var(--dim);height:300px;overflow-y:auto;white-space:pre-wrap;line-height:1.6}.log-box .done{color:var(--good)} .log-box .err{color:var(--bad)}.editor{width:100%;height:480px;background:var(--panel);color:var(--txt);border:1px solid var(--line);border-radius:10px;padding:16px;font-family:var(--mono,monospace);font-size:12.5px;line-height:1.6;resize:vertical}.editor:focus{outline:none;border-color:var(--line2)}.notice{padding:12px 16px;border-radius:8px;font-size:13px;margin-bottom:16px}.notice.info{background:rgba(126,200,227,.08);border:1px solid rgba(126,200,227,.2);color:#7ec8e3}.notice.ok{background:rgba(91,191,138,.08);border:1px solid rgba(91,191,138,.2);color:var(--good)}.notice.err{background:rgba(223,107,92,.08);border:1px solid rgba(223,107,92,.25);color:var(--bad)}.plan-row{display:flex;align-items:center;gap:10px;padding:10px 14px;border-bottom:1px solid var(--line);font-size:13px}.plan-row:last-child{border-bottom:none}.plan-row .sym{width:18px;font-weight:700;font-family:var(--mono,monospace)}.plan-row .sym.c{color:#7ec8e3} .plan-row .sym.u{color:var(--warn)}.plan-row .sym.s{color:var(--mut)} .plan-row .sym.d{color:var(--bad)}.plan-row .id{font-family:var(--mono,monospace);font-size:12px;min-width:160px}.plan-row .det{font-size:11px;color:var(--mut);margin-left:auto}.plan-table{background:var(--panel);border:1px solid var(--line);border-radius:10px;overflow:hidden}.stat-row{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:24px}.stat{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px 16px;min-width:120px}.stat .k{font-size:10px;color:var(--mut);font-family:var(--mono,monospace);letter-spacing:1px;text-transform:uppercase}.stat .v{font-size:22px;font-weight:700;font-family:var(--disp,sans-serif);margin-top:4px}.copy-text{padding:16px 20px;font-size:15px;line-height:1.65;color:var(--str);font-weight:500}.modal-bg{display:none;position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:100;align-items:center;justify-content:center}.modal-bg.open{display:flex}.modal{background:var(--panel2);border:1px solid var(--line2);border-radius:14px;padding:28px;max-width:580px;width:90%;max-height:80vh;overflow-y:auto}.modal h3{font-family:var(--disp);font-size:17px;font-weight:700;margin-bottom:16px}.modal .foot{display:flex;gap:8px;justify-content:flex-end;margin-top:20px}</style>
</head>
<body>
<nav class="nav">
  <div class="brand"><span class="dot"></span>BrandForge</div>
  <div class="tabs">
    <button class="tab active" data-page="pg-assets">Assets</button>
    <button class="tab" data-page="pg-apply">Apply</button>
    <button class="tab" data-page="pg-plan">Plan</button>
    <button class="tab" data-page="pg-spec">Spec</button>
  </div>
  <div class="right">
    <span id="status-bar" style="font-size:11px;color:var(--mut);font-family:monospace"></span>
  </div>
</nav>

<!-- Assets -->
<div id="pg-assets" class="page active">
  <div class="stat-row" style="margin-top:24px">
    <div class="stat"><div class="k">Total cost</div><div class="v" id="stat-cost">--</div></div>
  </div>
  <div id="assets-grid"></div>
</div>

<!-- Apply -->
<div id="pg-apply" class="page">
  <div class="section-head" style="margin-top:24px">
    <h2>Apply</h2>
    <div class="ml" style="display:flex;gap:8px">
      <button class="btn primary" id="apply-btn" onclick="runApply({})">Run Apply</button>
      <button class="btn" onclick="runApply({force:true})">Force All</button>
      <button class="btn danger" onclick="runPrune()">Prune Orphans</button>
    </div>
  </div>
  <div class="notice info" style="margin-bottom:16px">
    Apply generates all missing or drifted assets and updates state.
    Force regenerates everything regardless of state.
  </div>
  <pre class="log-box" id="apply-log"></pre>
</div>

<!-- Plan -->
<div id="pg-plan" class="page">
  <div class="section-head" style="margin-top:24px">
    <h2>Plan</h2>
    <code class="sub ml" id="plan-summary"></code>
    <button class="btn ml" style="margin-left:12px" onclick="loadPlan()">Refresh</button>
  </div>
  <div id="plan-list"><p style="color:var(--mut);font-size:13px">Click Refresh to diff the spec against state.</p></div>
</div>

<!-- Spec Editor -->
<div id="pg-spec" class="page">
  <div class="section-head" style="margin-top:24px">
    <h2>Spec Editor</h2>
    <div class="ml" style="display:flex;gap:8px">
      <button class="btn" onclick="loadSpec()">Reload</button>
      <button class="btn primary" onclick="saveSpec()">Save</button>
    </div>
  </div>
  <div id="spec-notice" class="notice" style="display:none"></div>
  <textarea class="editor" id="spec-editor" spellcheck="false"></textarea>
  <p style="margin-top:10px;font-size:12px;color:var(--mut)">
    Edit the brandspec JSON and click Save, then go to Apply to regenerate drifted assets.
  </p>
</div>

<script>
const $ = id => document.getElementById(id);
const API = async (method, path, body) => {
  const r = await fetch(path, {
    method, headers: {\'Content-Type\': \'application/json\'},
    body: body ? JSON.stringify(body) : undefined
  });
  if (!r.ok) { const t = await r.text(); throw new Error(t); }
  if (r.headers.get(\'content-type\')?.includes(\'json\')) return r.json();
  return r.text();
};

// Navigation
document.querySelectorAll(\'.tab\').forEach(t => {
  t.addEventListener(\'click\', () => {
    document.querySelectorAll(\'.tab\').forEach(x => x.classList.remove(\'active\'));
    document.querySelectorAll(\'.page\').forEach(x => x.classList.remove(\'active\'));
    t.classList.add(\'active\');
    $(t.dataset.page).classList.add(\'active\');
    if (t.dataset.page === \'pg-assets\') loadAssets();
    if (t.dataset.page === \'pg-plan\') loadPlan();
    if (t.dataset.page === \'pg-spec\') loadSpec();
  });
});

// ---- Assets page ----
async function loadAssets() {
  const state = await API(\'GET\', \'/api/state\');
  const container = $(\'assets-grid\');
  container.innerHTML = \'\';
  if (!state.records.length) {
    container.innerHTML = \'<p style="color:var(--mut);font-size:13px">No assets generated yet. Run Apply.</p>\';
    return;
  }
  const groups = {};
  state.records.forEach(r => { (groups[r.group] = groups[r.group] || []).push(r); });
  for (const [g, recs] of Object.entries(groups)) {
    const sec = document.createElement(\'div\');
    sec.className = \'group-section\';
    sec.innerHTML = \'<div class="section-head"><h2>\' + g + \'</h2>\' +
      \'<span class="sub ml">\' + recs.length + \' assets</span></div>\' +
      \'<div class="grid" id="grid-\' + g + \'"></div>\';
    container.appendChild(sec);
    const grid = sec.querySelector(\'.grid\');
    recs.forEach(r => grid.appendChild(makeCard(r)));
  }
  const cost = $(\'stat-cost\');
  if (cost) cost.textContent = \'$\' + (state.cost || 0).toFixed(4);
}

function badgeHtml(status) {
  const map = {completed:\'ok\',failed:\'bad\',needs_attention:\'warn\',pending:\'pending\'};
  return \'<span class="badge \' + (map[status]||\'pending\') + \'">\' + status.replace(\'_\',\' \') + \'</span>\';
}

function makeCard(r) {
  const card = document.createElement(\'div\');
  card.className = \'card\';
  card.dataset.id = r.asset_id;
  let mediaHtml = \'\';
  if (r.output_path && r.status === \'completed\') {
    const webPath = \'/out/\' + r.output_path.replace(/\\/g,\'/\').split(\'/out/\').pop();
    if (webPath.endsWith(\'.jpg\') || webPath.endsWith(\'.png\')) {
      const ratio = (r.group === \'hero\') ? \'wide\' : \'\';
      mediaHtml = \'<div class="media \' + ratio + \'"><img src="\' + webPath + \'" loading="lazy"></div>\';
    } else {
      fetch(webPath).then(x=>x.text()).then(t => {
        const stripped = t.replace(/<!--.*?-->/s,\'\').trim();
        card.querySelector(\'.copy-text\').textContent = stripped;
      });
      mediaHtml = \'<div class="copy-text">Loading...</div>\';
    }
  } else {
    const sym = {failed:\'!\',needs_attention:\'~\',pending:\'...\'}[r.status] || \'?\';
    mediaHtml = \'<div class="media"><span style="font-size:28px;color:var(--mut)">\' + sym + \'</span></div>\';
  }
  const tokens = (r.input_tokens||0) + (r.output_tokens||0);
  const meta = r.model + (tokens ? \' · \' + tokens + \' tok\' : \'\') + (r.cost ? \' · $\' + r.cost.toFixed(5) : \'\');
  card.innerHTML = mediaHtml +
    \'<div class="body">\' +
      \'<div class="title">\' + (r.label||r.asset_id) + \' \' + badgeHtml(r.status) + \'</div>\' +
      \'<div class="meta">\' + r.asset_id + \' · \' + meta + \'</div>\' +
      (r.failure_reason ? \'<div class="meta" style="color:var(--bad);margin-top:4px">\' + r.failure_reason + \'</div>\' : \'\') +
      \'<div class="actions">\' +
        \'<button class="btn" onclick="regenAsset(\\'\' + r.asset_id + \'\\')">Regen</button>\' +
        \'<button class="btn danger" onclick="deleteAsset(\\'\' + r.asset_id + \'\\')">Delete</button>\' +
      \'</div>\' +
    \'</div>\';
  return card;
}

async function regenAsset(id) {
  setStatus(\'Regenerating \' + id + \'...\');
  await runApply({select: [id], force: true});
}

async function deleteAsset(id) {
  if (!confirm(\'Delete \' + id + \' from state?\')) return;
  try {
    await API(\'DELETE\', \'/api/assets/\' + id);
    loadAssets();
    setStatus(\'Deleted \' + id);
  } catch(e) { alert(e.message); }
}

// ---- Plan page ----
async function loadPlan() {
  const container = $(\'plan-list\');
  container.innerHTML = \'<p style="color:var(--mut);font-size:13px">Loading...</p>\';
  try {
    const actions = await API(\'POST\', \'/api/plan\', {});
    if (!actions.length) { container.innerHTML = \'<p style="color:var(--mut)">Nothing to plan.</p>\'; return; }
    const symClass = {\'+\':\'c\',\'~\':\'u\',\'.\':\'s\',\'-\':\'d\'};
    container.innerHTML = \'<div class="plan-table">\' +
      actions.map(a => {
        const sc = symClass[a.symbol] || \'s\';
        return \'<div class="plan-row">\' +
          \'<span class="sym \' + sc + \'">\' + a.symbol + \'</span>\' +
          \'<span class="badge \' + a.action + \'">\' + a.action + \'</span>\' +
          \'<span class="id">\' + a.asset_id + \'</span>\' +
          \'<span style="font-size:12px;color:var(--dim)">\' + a.label + \'</span>\' +
          \'<span class="det">\' + a.model + \' · \' + a.detail + \'</span>\' +
        \'</div>\';
      }).join(\'\') + \'</div>\';
    const summary = actions.reduce((acc,a)=>{acc[a.action]=(acc[a.action]||0)+1;return acc;},{});
    $(\'plan-summary\').textContent =
      \'+\' + (summary.create||0) + \' create  \' +
      \'~\' + (summary.update||0) + \' update  \' +
      \'.\' + (summary.skip||0) + \' unchanged  \' +
      \'-\' + (summary.destroy||0) + \' destroy\';
  } catch(e) { container.innerHTML = \'<p style="color:var(--bad)">\' + e.message + \'</p>\'; }
}

// ---- Apply ----
async function runApply(opts) {
  const busy = await API(\'GET\', \'/api/status\');
  if (busy.busy) { alert(\'Apply already running\'); return; }
  $(\'apply-btn\').disabled = true;
  const log = $(\'apply-log\');
  log.innerHTML = \'\';
  const es = new EventSource(\'/api/apply\');
  const body = opts || {};
  // SSE must be POST; use fetch with ReadableStream instead
  es.close();
  const resp = await fetch(\'/api/apply\', {
    method: \'POST\',
    headers: {\'Content-Type\':\'application/json\'},
    body: JSON.stringify(body)
  });
  const reader = resp.body.getReader();
  const dec = new TextDecoder();
  let buf = \'\';
  while (true) {
    const {done, value} = await reader.read();
    if (done) break;
    buf += dec.decode(value, {stream: true});
    const lines = buf.split(\'\n\');
    buf = lines.pop();
    for (const line of lines) {
      if (!line.startsWith(\'data:\')) continue;
      const evt = JSON.parse(line.slice(5).trim());
      if (evt.type === \'log\') {
        log.innerHTML += evt.msg + \'\n\';
        log.scrollTop = log.scrollHeight;
      } else if (evt.type === \'done\') {
        log.innerHTML += \'<span class="done">Done. Cost: $\' + (evt.cost||0).toFixed(4) + \'</span>\n\';
        log.scrollTop = log.scrollHeight;
        loadAssets();
      } else if (evt.type === \'error\') {
        log.innerHTML += \'<span class="err">Error: \' + evt.msg + \'</span>\n\';
      }
    }
  }
  $(\'apply-btn\').disabled = false;
}

// ---- Spec editor ----
async function loadSpec() {
  try {
    const spec = await API(\'GET\', \'/api/spec\');
    $(\'spec-editor\').value = JSON.stringify(spec, null, 2);
  } catch(e) { setNotice(\'spec-notice\', e.message, \'err\'); }
}

async function saveSpec() {
  try {
    const parsed = JSON.parse($(\'spec-editor\').value);
    await API(\'PUT\', \'/api/spec\', {content: parsed});
    setNotice(\'spec-notice\', \'Spec saved.\', \'ok\');
  } catch(e) { setNotice(\'spec-notice\', e.message, \'err\'); }
}

// ---- Prune ----
async function runPrune() {
  if (!confirm(\'Delete state records and files for assets removed from the spec?\')) return;
  try {
    const r = await API(\'POST\', \'/api/prune\', {});
    alert(\'Pruned \' + r.removed.length + \' orphan(s).\');
    loadAssets();
  } catch(e) { alert(e.message); }
}

// ---- Helpers ----
function setStatus(msg) {
  const el = $(\'status-bar\');
  if (el) el.textContent = msg;
}

function setNotice(id, msg, type) {
  const el = $(id);
  if (!el) return;
  el.className = \'notice \' + type;
  el.textContent = msg;
  el.style.display = \'block\';
}

// Init
loadAssets();
</script>
</body>
</html>

'''
