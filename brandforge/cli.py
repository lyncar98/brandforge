"""Command-line interface — Brand-as-Code for creative assets.

    python -m brandforge doctor                 # check keys + SDKs
    python -m brandforge plan                    # diff the brandspec against state (no API calls)
    python -m brandforge apply  [--mock]         # generate what's missing/changed
    python -m brandforge gallery                  # (re)render the HTML gallery from state
    python -m brandforge status                   # print the run report

``--mock`` swaps in in-process fakes of both backends (Luma images + Claude
content), so the whole workflow runs with no key and zero spend — ideal for the
demo, CI, and reviewers.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import List, Optional

from .client import LumaAgentsClient
from .content import ClaudeContentClient, MockContentClient
from .errors import BrandForgeError
from .gallery import render_gallery
from .kit import KitConfig, KitRunner, load_kit
from .manifest import Manifest
from .mock import MockConfig, MockTransport
from .poller import PollConfig
from .retry import RetryPolicy

ENV_IMAGE_KEY = "LUMA_AGENTS_API_KEY"   # uni-1 images (official luma-agents SDK)
ENV_CONTENT_KEY = "ANTHROPIC_API_KEY"   # Claude content (official anthropic SDK)
_MOCK_KEY = "luma-api-mock-key"

DEFAULT_SPEC = "brands/lautum/brandspec.json"


def _load_env() -> None:
    """Best-effort load of a local .env so keys aren't required in the shell."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv()


def _build_clients(args: argparse.Namespace):
    """Returns (image_client, content_client). --mock uses in-process fakes."""
    if args.mock:
        return (
            LumaAgentsClient(_MOCK_KEY, transport=MockTransport(MockConfig(polls_until_complete=2))),
            MockContentClient(),
        )
    img_key = os.environ.get(ENV_IMAGE_KEY, "")
    txt_key = os.environ.get(ENV_CONTENT_KEY, "")
    if not img_key:
        raise BrandForgeError(f"{ENV_IMAGE_KEY} is not set (uni-1 images). Export it, or pass --mock.")
    if not txt_key:
        raise BrandForgeError(f"{ENV_CONTENT_KEY} is not set (Claude content). Export it, or pass --mock.")
    from .sdk_image import LumaSDKImageClient

    return LumaSDKImageClient(img_key), ClaudeContentClient(txt_key)


def _make_runner(args: argparse.Namespace, *, need_clients: bool = True) -> KitRunner:
    kit = load_kit(args.spec)
    if need_clients:
        image_client, content_client = _build_clients(args)
    else:
        image_client = content_client = None
    config = KitConfig(
        output_dir=args.out,
        max_concurrent=getattr(args, "concurrency", 3),
        rpm_limit=getattr(args, "rpm", 30),
        force=getattr(args, "force", False),
        retry=RetryPolicy(max_attempts=4, base_delay=1, max_delay=15) if args.mock
        else RetryPolicy(max_attempts=6, base_delay=2, max_delay=30),
        image_poll=PollConfig(
            initial_delay=0.0 if args.mock else 3.0,
            interval=0.05 if args.mock else 2.5,
            timeout=30.0 if args.mock else 120.0,
        ),
    )
    return KitRunner(image_client, content_client, kit, config, log=print)


def _selection(runner: KitRunner, args: argparse.Namespace):
    """Resolve --select / --group / --only into a set of asset ids, or None for all."""
    select = getattr(args, "select", None)
    group = getattr(args, "group", None)
    only = getattr(args, "only", None)
    if not select and not group and not only:
        return None
    ids = set()
    requested = set(select or [])
    for a in runner.kit.assets:
        if requested and a.id not in requested:
            continue
        if group and a.group != group:
            continue
        if only and a.kind != only:
            continue
        ids.add(a.id)
    unknown = requested - {a.id for a in runner.kit.assets}
    if unknown:
        raise BrandForgeError(f"unknown asset id(s): {', '.join(sorted(unknown))}")
    return ids


# -- commands -----------------------------------------------------------------

def _cmd_doctor(args: argparse.Namespace) -> int:
    if args.mock:
        print("mock mode: no real keys needed. Both backends run against in-process fakes.")
        return 0
    ok = True
    img = os.environ.get(ENV_IMAGE_KEY, "")
    if img:
        masked = img[:9] + "..." + img[-4:] if len(img) > 16 else "set"
        print(f"OK:   {ENV_IMAGE_KEY} present ({masked}) -> uni-1 images via luma-agents SDK")
    else:
        print(f"FAIL: {ENV_IMAGE_KEY} is not set (uni-1 images).", file=sys.stderr)
        ok = False

    txt = os.environ.get(ENV_CONTENT_KEY, "")
    if txt:
        masked = txt[:12] + "..." + txt[-4:] if len(txt) > 20 else "set"
        print(f"OK:   {ENV_CONTENT_KEY} present ({masked}) -> Claude content via anthropic SDK")
    else:
        print(f"FAIL: {ENV_CONTENT_KEY} is not set (Claude content).", file=sys.stderr)
        ok = False

    for mod, pkg in (("luma_agents", "luma-agents"), ("anthropic", "anthropic")):
        try:
            __import__(mod)
        except ImportError:
            print(f"FAIL: {pkg} is not installed (`pip install {pkg}`).", file=sys.stderr)
            ok = False
    return 0 if ok else 1


def _cmd_plan(args: argparse.Namespace) -> int:
    runner = _make_runner(args, need_clients=False)
    select = _selection(runner, args)
    manifest_path = Path(args.out) / "manifest.json"
    manifest = Manifest.load(manifest_path) if manifest_path.exists() else None
    actions = runner.plan(manifest, select=select)

    n = {k: sum(a.action == k for a in actions) for k in ("create", "update", "skip", "destroy")}
    scope = "" if select is None else f" (selection of {len(select)})"
    print(f"Plan for '{runner.kit.brand_name}'{scope}: "
          f"+{n['create']} create  ~{n['update']} update  .{n['skip']} unchanged  -{n['destroy']} destroy\n")
    for a in actions:
        kind = a.kind or "state"
        print(f"  {a.symbol} [{kind:7s} {a.group:10s} {a.model:16s}] {a.asset_id}: {a.label}  ({a.detail})")
    todo = n["create"] + n["update"]
    print(f"\nRun `apply` to converge {todo} asset(s)." +
          (f"  Run `prune` to remove {n['destroy']} orphan(s)." if n["destroy"] else "") +
          "  Add --mock for no key/spend.")
    return 0


def _cmd_prune(args: argparse.Namespace) -> int:
    kit = load_kit(args.spec)
    manifest_path = Path(args.out) / "manifest.json"
    if not manifest_path.exists():
        print(f"No state at {manifest_path}", file=sys.stderr)
        return 1
    manifest = Manifest.load(manifest_path)
    runner = KitRunner(None, None, kit, KitConfig(output_dir=args.out), log=print)
    removed = runner.prune(manifest)
    if not removed:
        print("Nothing to prune — state matches the spec.")
        return 0
    for a in removed:
        print(f"  - destroyed {a.asset_id} ({a.group})")
    manifest.save(manifest_path)
    render_gallery(kit, manifest, args.out)
    print(f"\nPruned {len(removed)} orphan(s); state and gallery updated.")
    return 0


def _cmd_apply(args: argparse.Namespace) -> int:
    runner = _make_runner(args)
    select = _selection(runner, args)
    manifest_path = Path(args.out) / "manifest.json"
    manifest = Manifest.load(manifest_path) if manifest_path.exists() else None
    manifest = runner.run(manifest=manifest, select=select)

    (Path(args.out) / "report.md").write_text(manifest.render_markdown(), encoding="utf-8")
    gallery = render_gallery(runner.kit, manifest, args.out)
    print(f"\nGallery: {gallery}")
    print(manifest.render_markdown())

    counts = manifest.counts()
    failed = counts["failed"] + counts["needs_attention"]
    if args.fail_on_error and failed:
        print(f"FAIL: {failed} asset(s) did not complete.", file=sys.stderr)
        return 1
    return 0


def _cmd_gallery(args: argparse.Namespace) -> int:
    kit = load_kit(args.spec)
    manifest_path = Path(args.out) / "manifest.json"
    if not manifest_path.exists():
        print(f"No state at {manifest_path}. Run `apply` first.", file=sys.stderr)
        return 1
    manifest = Manifest.load(manifest_path)
    path = render_gallery(kit, manifest, args.out)
    print(f"Gallery: {path}")
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    manifest_path = Path(args.out) / "manifest.json"
    if not manifest_path.exists():
        print(f"No state at {manifest_path}", file=sys.stderr)
        return 1
    print(Manifest.load(manifest_path).render_markdown())
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    try:
        import uvicorn
    except ImportError:
        print("error: uvicorn not installed — run: pip install uvicorn[standard]", file=sys.stderr)
        return 1
    from .server import create_app
    app = create_app(spec_path=args.spec, out_dir=args.out)
    print(f"BrandForge UI  →  http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


# -- parser -------------------------------------------------------------------

def _add_spec_out(p: argparse.ArgumentParser) -> None:
    p.add_argument("--spec", default=DEFAULT_SPEC, help="Brandspec JSON")
    p.add_argument("--out", default="out", help="Output directory (assets + state)")
    p.add_argument("--mock", action="store_true", help="Use in-process fakes (no key/spend)")


def _add_selection(p: argparse.ArgumentParser) -> None:
    p.add_argument("--select", nargs="*", metavar="ID", help="Only these asset id(s)")
    p.add_argument("--group", help="Only assets in this group (e.g. hero, domains)")
    p.add_argument("--only", choices=["image", "content"], help="Only this asset kind")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="brandforge",
        description="Manage a brand's image + content assets as code, on Luma uni-1 and Claude.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    d = sub.add_parser("doctor", help="Check API keys and SDKs.")
    d.add_argument("--mock", action="store_true")
    d.set_defaults(func=_cmd_doctor)

    pl = sub.add_parser("plan", help="Diff the brandspec against state (no API calls).")
    _add_spec_out(pl)
    _add_selection(pl)
    pl.add_argument("--force", action="store_true", help="Treat everything as pending")
    pl.set_defaults(func=_cmd_plan)

    ap = sub.add_parser("apply", help="Generate missing/changed assets and update state.")
    _add_spec_out(ap)
    _add_selection(ap)
    ap.add_argument("--concurrency", type=int, default=3, help="Max concurrent jobs")
    ap.add_argument("--rpm", type=int, default=30, help="Max submissions per minute")
    ap.add_argument("--force", action="store_true", help="Regenerate everything (ignore drift/state)")
    ap.add_argument("--fail-on-error", action="store_true", help="Exit non-zero if any asset fails")
    ap.set_defaults(func=_cmd_apply)

    pr = sub.add_parser("prune", help="Delete assets (and files) that are no longer in the spec.")
    pr.add_argument("--spec", default=DEFAULT_SPEC, help="Brandspec JSON")
    pr.add_argument("--out", default="out", help="Output directory (assets + state)")
    pr.set_defaults(func=_cmd_prune)

    g = sub.add_parser("gallery", help="(Re)render the HTML gallery from state.")
    _add_spec_out(g)
    g.set_defaults(func=_cmd_gallery)

    s = sub.add_parser("status", help="Print the run report from state.")
    s.add_argument("--out", default="out")
    s.set_defaults(func=_cmd_status)

    sv = sub.add_parser("serve", help="Start the full-CRUD web UI (FastAPI + Uvicorn).")
    _add_spec_out(sv)
    sv.add_argument("--port", type=int, default=7860, help="Port (default 7860)")
    sv.add_argument("--host", default="127.0.0.1", help="Bind host (default 127.0.0.1)")
    sv.set_defaults(func=_cmd_serve)

    return p


def main(argv: Optional[List[str]] = None) -> int:
    _load_env()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except BrandForgeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
