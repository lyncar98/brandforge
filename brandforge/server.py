"""BrandForge FastAPI backend.

Exposes every CLI capability (plan / apply / prune / status) over a REST + SSE
API. In production the built React SPA is served from brandforge/static/; in
development Vite's dev server proxies /api and /out here.

Start (dev):
    uvicorn brandforge.server:app --reload --port 8000
Or via CLI:
    python -m brandforge serve --port 8000
"""
from __future__ import annotations

import json
import os
import queue
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .gallery import render_gallery
from .kit import BrandKit, KitConfig, KitRunner, load_kit
from .manifest import Manifest, job_key

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_HERE = Path(__file__).parent
_STATIC = _HERE / "static"          # built React bundle
_DEFAULT_SPEC = Path("brands/lautum/brandspec.json")
_DEFAULT_OUT  = Path("out")


def _normalise_asset_path(raw: str | None, out_dir: Path) -> str | None:
    """Return a URL path like /out/domains/foo.jpg from any raw output_path."""
    if not raw:
        return None
    p = Path(raw.replace("\\", "/"))
    try:
        rel = p.relative_to(out_dir)
    except ValueError:
        # Stored path might itself start with "out/"
        parts = p.parts
        try:
            idx = parts.index("out")
            rel = Path(*parts[idx + 1:])
        except (ValueError, TypeError):
            rel = p
    return "/out/" + rel.as_posix()


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


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

class _AppState:
    def __init__(self, spec_path: Path, out_dir: Path) -> None:
        self.spec_path = spec_path
        self.out_dir   = out_dir
        self._run_thread: Optional[threading.Thread] = None

    @property
    def manifest_path(self) -> Path:
        return self.out_dir / "manifest.json"

    def load_kit(self) -> BrandKit:
        return load_kit(self.spec_path)

    def load_manifest(self) -> Optional[Manifest]:
        return Manifest.load(self.manifest_path) if self.manifest_path.exists() else None

    def is_busy(self) -> bool:
        return self._run_thread is not None and self._run_thread.is_alive()

    def _make_runner(self, kit: BrandKit, log=None) -> KitRunner:
        from .client import LumaAgentsClient
        from .content import ClaudeContentClient, MockContentClient
        from .mock import MockTransport, MockConfig

        luma_key = os.getenv("LUMA_AGENTS_API_KEY", "")
        ant_key  = os.getenv("ANTHROPIC_API_KEY", "")
        mock = not luma_key or not ant_key
        if mock:
            ic = LumaAgentsClient("mock", transport=MockTransport(MockConfig()))
            cc = MockContentClient()
        else:
            ic = LumaAgentsClient(luma_key)
            cc = ClaudeContentClient(ant_key)
        config = KitConfig(output_dir=str(self.out_dir))
        return KitRunner(ic, cc, kit, config, log=log or (lambda m: None))


def create_app(
    spec_path: str | Path = _DEFAULT_SPEC,
    out_dir: str | Path   = _DEFAULT_OUT,
) -> FastAPI:
    spec_path = Path(spec_path)
    out_dir   = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    app = FastAPI(title="BrandForge", version="0.2.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
    )
    st = _AppState(spec_path=spec_path, out_dir=out_dir)
    _register_routes(app, st)
    return app


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def _register_routes(app: FastAPI, st: _AppState) -> None:  # noqa: C901

    # ---- spec ---------------------------------------------------------------

    @app.get("/api/spec")
    def get_spec() -> JSONResponse:
        if not st.spec_path.exists():
            raise HTTPException(404, "Spec file not found")
        return JSONResponse(json.loads(st.spec_path.read_text("utf-8")))

    class SpecBody(BaseModel):
        content: Dict[str, Any]

    @app.put("/api/spec")
    def put_spec(body: SpecBody) -> Dict:
        try:
            BrandKit.from_dict(body.content)
        except Exception as exc:
            raise HTTPException(422, str(exc))
        st.spec_path.write_text(
            json.dumps(body.content, indent=2, ensure_ascii=False), "utf-8"
        )
        return {"ok": True}

    # ---- state --------------------------------------------------------------

    @app.get("/api/state")
    def get_state() -> JSONResponse:
        m = st.load_manifest()
        if m is None:
            return JSONResponse({"records": [], "cost": 0.0, "counts": {}})
        records = [
            {
                "key": r.key,
                "asset_id": r.key.split("::")[-1],
                "group": r.product_id,
                "label": r.channel,
                "status": r.status,
                "model": r.model,
                "output_path": _normalise_asset_path(r.output_path, st.out_dir),
                "failure_code": r.failure_code,
                "failure_reason": r.failure_reason,
                "input_tokens": r.input_tokens,
                "output_tokens": r.output_tokens,
                "cost": r.estimated_cost(),
                "updated_at": r.updated_at,
            }
            for r in m.records.values()
        ]
        return JSONResponse({"records": records, "cost": m.estimated_cost(), "counts": m.counts()})

    # ---- plan ---------------------------------------------------------------

    @app.post("/api/plan")
    def post_plan(body: Optional[Dict[str, Any]] = None) -> JSONResponse:
        kit = st.load_kit()
        m   = st.load_manifest()
        runner = st._make_runner(kit)
        actions = runner.plan(m, select=_resolve_select(kit, body))
        return JSONResponse([
            {
                "asset_id": a.asset_id, "kind": a.kind,
                "group": a.group, "label": a.label,
                "action": a.action, "model": a.model,
                "detail": a.detail, "symbol": a.symbol,
            }
            for a in actions
        ])

    # ---- apply (SSE) --------------------------------------------------------

    @app.post("/api/apply")
    def post_apply(body: Optional[Dict[str, Any]] = None) -> StreamingResponse:
        if st.is_busy():
            raise HTTPException(409, "An apply is already running")
        kit    = st.load_kit()
        select = _resolve_select(kit, body)
        force  = bool((body or {}).get("force", False))
        log_q: queue.Queue = queue.Queue()

        def _run() -> None:
            try:
                kit2     = st.load_kit()
                manifest = st.load_manifest()

                def _log(msg: str) -> None:
                    log_q.put({"type": "log", "msg": msg})

                runner = st._make_runner(kit2, log=_log)
                runner.config.force = force
                manifest = runner.run(manifest=manifest, select=select)
                render_gallery(kit2, manifest, st.out_dir)
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

    # ---- status -------------------------------------------------------------

    @app.get("/api/status")
    def get_status() -> Dict:
        return {"busy": st.is_busy()}

    # ---- prune --------------------------------------------------------------

    @app.post("/api/prune")
    def post_prune() -> Dict:
        if st.is_busy():
            raise HTTPException(409, "Apply is running")
        m = st.load_manifest()
        if m is None:
            return {"removed": []}
        kit    = st.load_kit()
        runner = st._make_runner(kit)
        removed = runner.prune(m)
        m.save(st.manifest_path)
        if removed:
            render_gallery(kit, m, st.out_dir)
        return {"removed": [{"asset_id": a.asset_id, "group": a.group} for a in removed]}

    # ---- delete one asset ---------------------------------------------------

    @app.delete("/api/assets/{asset_id}")
    def delete_asset(asset_id: str) -> Dict:
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

    # ---- generated asset files ----------------------------------------------

    app.mount("/out", StaticFiles(directory=str(st.out_dir)), name="out")

    # ---- React SPA (production) or dev fallback ----------------------------

    if _STATIC.exists():
        app.mount("/assets", StaticFiles(directory=str(_STATIC / "assets")), name="spa-assets")

        @app.get("/", include_in_schema=False)
        @app.get("/{catchall:path}", include_in_schema=False)
        def spa(catchall: str = "") -> FileResponse:
            # Let static assets through; serve index.html for everything else
            candidate = _STATIC / catchall
            if candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(_STATIC / "index.html")
    else:
        @app.get("/", include_in_schema=False)
        def dev_root() -> Response:
            return Response(
                "<p>Run <code>cd frontend && npm run dev</code> then open "
                "<a href='http://localhost:5173'>localhost:5173</a></p>",
                media_type="text/html",
            )


# Default app instance (used by `uvicorn brandforge.server:app`)
app = create_app()
