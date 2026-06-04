"""Render a branded HTML gallery of a brandspec run.

Self-contained and on-brand: it uses the exact Lautum monochrome token set and
type system (Archivo / Manrope / Spline Sans Mono), a crisp digital surface with
fine gridlines and zero texture — the look of the product itself. Image assets
render as ``<img>``; content assets render their generated copy in a typeset
card. Failed / needs-attention assets render as a labelled status placeholder so
the gallery doubles as a state view.
"""

from __future__ import annotations

import html
import os
from pathlib import Path
from typing import TYPE_CHECKING

from .manifest import JobStatus, Manifest, job_key

if TYPE_CHECKING:
    from .kit import BrandKit

_CSS = """
:root{
  --ink:#000;--panel:#0a0a0a;--panel-2:#121212;--line:#242424;--line-2:#333;--line-soft:#1a1a1a;
  --mut:#8a8a8a;--mut-2:#5e5e5e;--txt:#f5f5f5;--txt-dim:#bdbdbd;--t-strong:#fff;
  --good:#5bbf8a;--warn:#d6a44a;--alert:#df6b5c;
  --disp:'Archivo','Manrope',system-ui,sans-serif;--sans:'Manrope',system-ui,sans-serif;--mono:'Spline Sans Mono',ui-monospace,monospace;
}
*{box-sizing:border-box;margin:0;padding:0}
html{scroll-behavior:smooth}
body{background:var(--ink);color:var(--txt);font-family:var(--sans);-webkit-font-smoothing:antialiased;line-height:1.5;padding-bottom:80px}
.nav{position:sticky;top:0;z-index:20;display:flex;align-items:center;gap:14px;padding:18px 40px;border-bottom:1px solid var(--line);background:rgba(0,0,0,.82);backdrop-filter:blur(12px)}
.brand{display:flex;align-items:center;gap:10px;font-family:var(--disp);font-weight:800;letter-spacing:-.3px;font-size:19px}
.brand .dot{width:11px;height:11px;border:1.5px solid var(--t-strong);border-radius:3px;transform:rotate(45deg)}
.nav .meta{margin-left:auto;font-family:var(--mono);font-size:11px;color:var(--mut);letter-spacing:.4px}
.hero{position:relative;overflow:hidden;border-bottom:1px solid var(--line);padding:64px 40px 40px}
.gridlines{position:absolute;inset:0;background-image:linear-gradient(var(--line-soft) 1px,transparent 1px),linear-gradient(90deg,var(--line-soft) 1px,transparent 1px);background-size:46px 46px;mask-image:radial-gradient(680px 360px at 75% -10%,#000 0%,transparent 70%);opacity:.7}
.hero-in{position:relative;max-width:1200px;margin:0 auto}
.kicker{font-family:var(--mono);font-size:11px;letter-spacing:2.4px;text-transform:uppercase;color:var(--mut)}
h1{font-family:var(--disp);font-weight:800;font-size:clamp(28px,4.4vw,50px);letter-spacing:-1px;margin:16px 0 12px;line-height:1.02}
.lead{color:var(--txt-dim);max-width:640px;font-size:15.5px}
.lead b{color:var(--txt);font-weight:600}
.stats{display:flex;gap:10px;flex-wrap:wrap;margin-top:24px}
.stat{border:1px solid var(--line);border-radius:10px;padding:10px 14px;background:linear-gradient(180deg,#141414,#0d0d0d)}
.stat .k{font-family:var(--mono);font-size:10px;letter-spacing:1.2px;text-transform:uppercase;color:var(--mut)}
.stat .v{font-family:var(--disp);font-weight:700;font-size:19px;margin-top:3px}
.wrap{max-width:1200px;margin:0 auto;padding:0 40px}
.group{margin-top:46px}
.group-h{display:flex;align-items:baseline;gap:12px;border-bottom:1px solid var(--line);padding-bottom:12px;margin-bottom:20px}
.group-h h2{font-family:var(--disp);font-weight:700;font-size:17px;letter-spacing:-.2px}
.group-h .gk{font-family:var(--mono);font-size:10.5px;letter-spacing:1.4px;text-transform:uppercase;color:var(--mut);margin-left:auto}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:18px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:14px;overflow:hidden;transition:border-color .18s ease,transform .18s ease}
.card:hover{border-color:var(--line-2);transform:translateY(-2px)}
.media{aspect-ratio:16/10;background:var(--ink);display:flex;align-items:center;justify-content:center;border-bottom:1px solid var(--line)}
.media img{width:100%;height:100%;object-fit:cover;display:block}
.copy{padding:22px;min-height:170px;display:flex;align-items:center;border-bottom:1px solid var(--line);background:radial-gradient(420px 200px at 80% -20%,#101010,transparent 60%)}
.copy .q{font-family:var(--disp);font-weight:600;font-size:17px;letter-spacing:-.2px;color:var(--t-strong);line-height:1.4}
.copy .q.sm{font-family:var(--sans);font-weight:400;font-size:14px;color:var(--txt-dim);letter-spacing:0}
.ph{font-family:var(--mono);font-size:11px;color:var(--mut);text-align:center;padding:24px;line-height:1.6}
.cb{padding:15px 18px}
.cb .lab{font-weight:600;font-size:14px;margin-bottom:5px}
.cb .pr{font-size:12px;color:var(--mut);line-height:1.5;max-height:54px;overflow:hidden}
.row{display:flex;align-items:center;gap:7px;margin-top:13px;flex-wrap:wrap}
.tag{font-family:var(--mono);font-size:10px;padding:3px 9px;border-radius:6px;border:1px solid var(--line-2);color:var(--txt-dim);letter-spacing:.3px}
.st{font-family:var(--mono);font-size:10px;padding:3px 9px;border-radius:6px;margin-left:auto;letter-spacing:.3px}
.st.ok{color:var(--good);border:1px solid rgba(91,191,138,.4)}
.st.warn{color:var(--warn);border:1px solid rgba(214,164,74,.4)}
.st.bad{color:var(--alert);border:1px solid rgba(223,107,92,.45)}
.foot{max-width:1200px;margin:56px auto 0;padding:24px 40px 0;border-top:1px solid var(--line);color:var(--mut);font-size:12px;display:flex;gap:14px;flex-wrap:wrap;align-items:center;font-family:var(--mono)}
.foot .sp{margin-left:auto}
"""


def _rel(output_path: str, out_dir: Path) -> str:
    return os.path.relpath(output_path, out_dir).replace(os.sep, "/")


def _read_content(path: str) -> str:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return ""
    # Strip the provenance comment header the runner writes.
    lines = [ln for ln in text.splitlines() if not ln.startswith("<!--")]
    return "\n".join(lines).strip()


def render_gallery(kit: "BrandKit", manifest: Manifest, out_dir: str | Path) -> Path:
    out_dir = Path(out_dir)
    groups: dict[str, list] = {}
    for asset in kit.assets:
        groups.setdefault(asset.group, []).append(asset)

    counts = manifest.counts()
    total = len(kit.assets)
    n_img = sum(a.kind == "image" for a in kit.assets)
    n_txt = sum(a.kind == "content" for a in kit.assets)

    body = []
    for group, assets in groups.items():
        body.append(
            f'<section class="group"><div class="group-h">'
            f'<h2>{html.escape(group.replace("_", " ").title())}</h2>'
            f'<span class="gk">{len(assets)} assets</span></div><div class="grid">'
        )
        for a in assets:
            rec = manifest.get(job_key(kit.brand_name, a.id))
            status = rec.status if rec else JobStatus.PENDING
            is_content = a.kind == "content"
            model = a.default_model()

            if status == JobStatus.COMPLETED and rec and rec.output_path:
                if is_content:
                    text = _read_content(rec.output_path)
                    ct = a.params.get("content_type", "copy")
                    short = ct in ("tagline", "social_post", "social", "meta_description", "alt_text")
                    media = (f'<div class="copy"><p class="q{" sm" if not short else ""}">'
                             f'{html.escape(text)}</p></div>')
                else:
                    src = _rel(rec.output_path, out_dir)
                    media = f'<div class="media"><img src="{html.escape(src)}" alt="{html.escape(a.label or a.id)}"></div>'
                st = '<span class="st ok">applied</span>'
            else:
                reason = (rec.failure_reason or rec.failure_code) if rec else "pending"
                media = f'<div class="ph">{html.escape(a.kind)} · {html.escape(str(reason))}</div>'
                cls = "warn" if status == JobStatus.NEEDS_ATTENTION else "bad"
                st = f'<span class="st {cls}">{html.escape(status)}</span>'

            type_tag = (a.params.get("content_type", "content") if is_content
                        else a.params.get("aspect_ratio", "auto"))
            body.append(
                f'<div class="card">{media}<div class="cb">'
                f'<div class="lab">{html.escape(a.label or a.id)}</div>'
                f'<div class="pr">{html.escape(a.prompt)}</div>'
                f'<div class="row"><span class="tag">{html.escape(model)}</span>'
                f'<span class="tag">{html.escape(str(type_tag))}</span>{st}</div>'
                f'</div></div>'
            )
        body.append("</div></section>")

    doc = f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(kit.brand_name)} — brand assets, as code</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Manrope:wght@400;500;600;700;800&family=Archivo:wght@600;700;800&family=Spline+Sans+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>{_CSS}</style></head><body>
<nav class="nav"><span class="brand"><span class="dot"></span>{html.escape(kit.brand_name)}</span>
<span class="meta">{counts[JobStatus.COMPLETED]}/{total} applied · run {html.escape(manifest.run_id)}</span></nav>
<header class="hero"><div class="gridlines"></div><div class="hero-in">
<span class="kicker">Brand assets · managed as code · Luma uni-1 + Claude</span>
<h1>{html.escape(kit.brand_name)} — the brand, compiled.</h1>
<p class="lead">Every asset below is declared in one versioned <b>brandspec</b> and generated through a single
<b>plan / apply</b> workflow — stills on <b>uni-1</b>, copy on <b>Claude</b>. Change the spec, re-apply, ship.
Estimated spend this run: <b>~${manifest.estimated_cost():.4f}</b> (placeholder rates).</p>
<div class="stats">
<div class="stat"><div class="k">Assets</div><div class="v">{total}</div></div>
<div class="stat"><div class="k">Images</div><div class="v">{n_img}</div></div>
<div class="stat"><div class="k">Content</div><div class="v">{n_txt}</div></div>
<div class="stat"><div class="k">Applied</div><div class="v">{counts[JobStatus.COMPLETED]}</div></div>
</div></div></header>
<div class="wrap">{''.join(body)}</div>
<div class="foot"><span>{html.escape(kit.brand_name)} brandspec</span>
<span>Generated with Luma uni-1 + Anthropic Claude</span><span class="sp">brandforge</span></div>
</body></html>"""

    path = out_dir / "gallery.html"
    path.write_text(doc, encoding="utf-8")
    return path
