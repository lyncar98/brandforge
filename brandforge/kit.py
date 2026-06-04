"""The brandspec runner — BrandForge's core, Brand-as-Code workflow.

A **brandspec** is a single declarative file describing the assets a brand needs,
checked into version control like any other source:

* ``kind: "image"``   -> a still, generated on Luma ``uni-1`` / ``uni-1-max``.
* ``kind: "content"`` -> a piece of copy, generated on Anthropic Claude.

The spec carries shared brand identity — a ``style`` string appended to every
image prompt and a ``voice`` string handed to every content brief — so the whole
kit stays on-brand by construction. The runner is the *apply* step: it diffs the
spec against the run **state** (``manifest.json``), generates only what's missing
or changed, and records the result. Re-running is idempotent and cheap.

The resilient machinery — bounded concurrency, RPM limiting, retries that honour
the retryable/terminal split, poll-to-terminal for images, download-before the
presigned URL expires, and the resumable state file — is shared across both
modalities. Adding content didn't mean a second pipeline; it meant a second
backend that the same orchestrator drives.
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Callable, Dict, List, Optional

from .content import ContentRequest
from .download import download_output
from .errors import (
    APIError,
    AuthError,
    BrandForgeError,
    GenerationFailed,
    InsufficientCreditsError,
    InvalidRequestError,
    PollTimeout,
    RateLimitError,
)
from .manifest import JobRecord, JobStatus, Manifest, job_key
from .models import GenerationRequest, ImageRef, Model, OutputFormat, State, Style
from .poller import PollConfig, poll_until_terminal
from .ratelimit import RpmLimiter
from .retry import RetryPolicy, call_with_retry

Logger = Callable[[str], None]

IMAGE = "image"
CONTENT = "content"


def _noop(_: str) -> None:
    pass


@dataclass
class KitAsset:
    id: str
    kind: str = IMAGE                 # "image" | "content"
    prompt: str = ""                  # image: the prompt; content: the brief
    group: str = "assets"
    label: str = ""
    params: Dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: Dict) -> "KitAsset":
        known = {"id", "kind", "prompt", "brief", "group", "label"}
        # ``brief`` is the natural word for content; accept it as an alias of prompt.
        prompt = d.get("prompt", d.get("brief", ""))
        return cls(
            id=d["id"],
            kind=d.get("kind", IMAGE),
            prompt=prompt,
            group=d.get("group", "assets"),
            label=d.get("label", ""),
            params={k: v for k, v in d.items() if k not in known},
        )

    @property
    def is_content(self) -> bool:
        return self.kind == CONTENT

    def default_model(self) -> str:
        if self.is_content:
            from .content import DEFAULT_MODEL
            return self.params.get("model", DEFAULT_MODEL)
        return self.params.get("model", "uni-1")


@dataclass
class BrandKit:
    brand_name: str
    style: str = ""                   # appended to every image prompt
    voice: str = ""                   # handed to every content brief
    assets: List[KitAsset] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: Dict) -> "BrandKit":
        return cls(
            brand_name=d["brand_name"],
            style=d.get("style", ""),
            voice=d.get("voice", ""),
            assets=[KitAsset.from_dict(a) for a in d.get("assets", [])],
        )


def load_kit(path: str | Path) -> BrandKit:
    p = Path(path)
    if not p.exists():
        raise BrandForgeError(f"brandspec file not found: {p}")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BrandForgeError(f"invalid JSON in {p}: {exc}") from exc
    kit = BrandKit.from_dict(data)
    if not kit.assets:
        raise BrandForgeError("brandspec has no assets")
    ids = [a.id for a in kit.assets]
    if len(ids) != len(set(ids)):
        raise BrandForgeError("brandspec asset ids must be unique")
    for a in kit.assets:
        if a.kind not in (IMAGE, CONTENT):
            raise BrandForgeError(f"asset {a.id!r}: kind must be 'image' or 'content' (got {a.kind!r})")
        if not a.prompt:
            raise BrandForgeError(f"asset {a.id!r}: missing 'prompt'/'brief'")
    return kit


@dataclass
class PlannedAction:
    """One line of a ``plan``: what apply would do to this asset, and why."""

    asset_id: str
    kind: str
    group: str
    label: str
    action: str            # "create" | "skip"
    model: str
    detail: str = ""


@dataclass
class KitConfig:
    output_dir: str = "out"
    max_concurrent: int = 3
    rpm_limit: int = 30
    max_job_retries: int = 2
    force: bool = False
    image_poll: PollConfig = field(default_factory=PollConfig)
    retry: RetryPolicy = field(default_factory=RetryPolicy)


class KitRunner:
    def __init__(
        self,
        image_client,
        content_client,
        kit: BrandKit,
        config: Optional[KitConfig] = None,
        *,
        fetch_image: Optional[Callable[[str], bytes]] = None,
        log: Logger = _noop,
    ) -> None:
        self.image_client = image_client
        self.content_client = content_client
        self.kit = kit
        self.config = config or KitConfig()
        self.fetch_image = fetch_image or (image_client.get_output_bytes if image_client else None)
        self.log = log
        self._rpm = RpmLimiter(self.config.rpm_limit)
        self._lock = Lock()

    # -- planning -------------------------------------------------------------

    def plan(self, manifest: Optional[Manifest] = None) -> List[PlannedAction]:
        """Diff the spec against state without calling any API (terraform-style)."""
        actions: List[PlannedAction] = []
        for a in self.kit.assets:
            key = job_key(self.kit.brand_name, a.id)
            satisfied = manifest is not None and not self.config.force and manifest.is_satisfied(key)
            actions.append(
                PlannedAction(
                    asset_id=a.id, kind=a.kind, group=a.group, label=a.label or a.id,
                    action="skip" if satisfied else "create",
                    model=a.default_model(),
                    detail="already in state" if satisfied else self._dest_for(a).name,
                )
            )
        return actions

    # -- request building -----------------------------------------------------

    def _final_prompt(self, asset: KitAsset) -> str:
        if not self.kit.style:
            return asset.prompt
        return f"{asset.prompt}. {self.kit.style}".strip().rstrip(".") + "."

    def build_image_request(self, asset: KitAsset) -> GenerationRequest:
        p = asset.params
        return GenerationRequest(
            prompt=self._final_prompt(asset),
            model=Model(p.get("model", "uni-1")),
            aspect_ratio=p.get("aspect_ratio"),
            style=Style(p.get("style", "auto")),
            output_format=OutputFormat(p["output_format"]) if p.get("output_format") else None,
            image_ref=[ImageRef(url=u) for u in p.get("style_reference_urls", [])][:9],
            web_search=bool(p.get("web_search", False)),
        ).validate()

    def build_content_request(self, asset: KitAsset) -> ContentRequest:
        from .content import DEFAULT_MODEL
        p = asset.params
        return ContentRequest(
            brief=asset.prompt,
            content_type=p.get("content_type", "copy"),
            voice=self.kit.voice,
            brand_name=self.kit.brand_name,
            model=p.get("model", DEFAULT_MODEL),
            max_words=p.get("max_words"),
            max_tokens=int(p.get("max_tokens", 1024)),
            temperature=float(p.get("temperature", 0.7)),
        ).validate()

    def _dest_for(self, asset: KitAsset) -> Path:
        ext = self._ext_for(asset)
        return Path(self.config.output_dir) / asset.group / f"{asset.id}{ext}"

    @staticmethod
    def _ext_for(asset: KitAsset) -> str:
        if asset.is_content:
            return ".md"
        fmt = asset.params.get("output_format")
        return ".jpg" if fmt == "jpeg" else ".png"

    # -- run ------------------------------------------------------------------

    def run(self, manifest: Optional[Manifest] = None) -> Manifest:
        out_dir = Path(self.config.output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        manifest = manifest or Manifest(brand_name=self.kit.brand_name, output_dir=str(out_dir))
        manifest_path = out_dir / "manifest.json"

        n_img = sum(a.kind == IMAGE for a in self.kit.assets)
        n_txt = sum(a.kind == CONTENT for a in self.kit.assets)
        self.log(f"brandspec '{self.kit.brand_name}': {len(self.kit.assets)} assets "
                 f"({n_img} image, {n_txt} content)")

        todo = []
        for a in self.kit.assets:
            key = job_key(self.kit.brand_name, a.id)
            if not self.config.force and manifest.is_satisfied(key):
                self.log(f"skip   {a.id} (already in state)")
                continue
            todo.append(a)

        if todo:
            with ThreadPoolExecutor(max_workers=self.config.max_concurrent) as pool:
                futures = {pool.submit(self._process, a): a for a in todo}
                for fut in as_completed(futures):
                    asset = futures[fut]
                    try:
                        rec = fut.result()
                    except (AuthError, InsufficientCreditsError) as exc:
                        self.log(f"ABORT: {exc}")
                        for f in futures:
                            f.cancel()
                        manifest.save(manifest_path)
                        raise
                    except Exception as exc:  # noqa: BLE001 — never let one asset kill the run
                        rec = JobRecord(
                            key=job_key(self.kit.brand_name, asset.id),
                            product_id=asset.group, channel=asset.label or asset.kind,
                            prompt=self._final_prompt(asset) if asset.kind == IMAGE else asset.prompt,
                            model=asset.default_model(),
                        )
                        rec.status, rec.failure_code, rec.failure_reason = (
                            JobStatus.FAILED, "unexpected_error", str(exc))
                    with self._lock:
                        manifest.upsert(rec)
                        manifest.save(manifest_path)
                    self.log(f"{rec.status:15s} {rec.key.split('::')[-1]}")

        manifest.save(manifest_path)
        return manifest

    # -- per-asset processing -------------------------------------------------

    def _process(self, asset: KitAsset) -> JobRecord:
        if asset.is_content:
            return self._process_content(asset)
        return self._process_image(asset)

    def _process_content(self, asset: KitAsset) -> JobRecord:
        key = job_key(self.kit.brand_name, asset.id)
        rec = JobRecord(
            key=key, product_id=asset.group, channel=asset.label or asset.kind,
            prompt=asset.prompt, model=asset.default_model(),
        )
        try:
            req = self.build_content_request(asset)
        except InvalidRequestError as exc:
            rec.status, rec.failure_code, rec.failure_reason = (
                JobStatus.NEEDS_ATTENTION, "invalid_request", str(exc))
            return rec

        for attempt in range(1, self.config.max_job_retries + 2):
            rec.attempts = attempt
            try:
                result = call_with_retry(
                    lambda: (self._rpm.acquire(), self.content_client.create_content(req))[1],
                    self.config.retry,
                )
            except (AuthError, InsufficientCreditsError):
                raise
            except GenerationFailed as exc:
                rec.failure_code, rec.failure_reason = exc.failure_code, exc.failure_reason
                if exc.retryable and attempt <= self.config.max_job_retries:
                    self.log(f"retry  {asset.id} after {exc.failure_code} (attempt {attempt})")
                    continue
                rec.status = (JobStatus.NEEDS_ATTENTION
                              if exc.failure_code in {"content_moderated", "budget_exhausted"}
                              else JobStatus.FAILED)
                return rec
            except RateLimitError as exc:
                if attempt <= self.config.max_job_retries:
                    self.log(f"retry  {asset.id} after rate limit (attempt {attempt})")
                    time.sleep(min(30.0, 5.0 * attempt))
                    continue
                rec.status, rec.failure_code, rec.failure_reason = (
                    JobStatus.NEEDS_ATTENTION, "rate_limited", str(exc))
                return rec
            except APIError as exc:
                if getattr(exc, "retryable", False) and attempt <= self.config.max_job_retries:
                    self.log(f"retry  {asset.id} after {type(exc).__name__} (attempt {attempt})")
                    continue
                rec.status, rec.failure_code, rec.failure_reason = (
                    JobStatus.FAILED, "api_error", str(exc))
                return rec

            dest = self._dest_for(asset)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(self._render_content(asset, result), encoding="utf-8")
            rec.status = JobStatus.COMPLETED
            rec.output_path = str(dest)
            rec.generation_id = result.request_id
            rec.input_tokens = result.input_tokens
            rec.output_tokens = result.output_tokens
            rec.model = result.model
            return rec

        rec.status = JobStatus.FAILED  # pragma: no cover
        return rec

    def _render_content(self, asset: KitAsset, result) -> str:
        ct = asset.params.get("content_type", "copy")
        header = f"<!-- {self.kit.brand_name} · {asset.label or asset.id} · {ct} · {result.model} -->\n"
        return header + result.text + "\n"

    def _process_image(self, asset: KitAsset) -> JobRecord:
        key = job_key(self.kit.brand_name, asset.id)
        rec = JobRecord(
            key=key, product_id=asset.group, channel=asset.label or asset.kind,
            prompt=self._final_prompt(asset),
            model=asset.default_model(),
            aspect_ratio=asset.params.get("aspect_ratio"),
        )
        try:
            req = self.build_image_request(asset)
        except InvalidRequestError as exc:
            rec.status, rec.failure_code, rec.failure_reason = (
                JobStatus.NEEDS_ATTENTION, "invalid_request", str(exc))
            return rec

        fmt = OutputFormat(asset.params["output_format"]) if asset.params.get("output_format") else None
        ext = ".jpg" if fmt is OutputFormat.JPEG else ".png"

        for attempt in range(1, self.config.max_job_retries + 2):
            rec.attempts = attempt
            try:
                gen = call_with_retry(
                    lambda: (self._rpm.acquire(), self.image_client.create_generation(req))[1],
                    self.config.retry,
                )
            except (AuthError, InsufficientCreditsError):
                raise
            except RateLimitError as exc:
                if attempt <= self.config.max_job_retries:
                    self.log(f"retry  {asset.id} after rate limit (attempt {attempt})")
                    time.sleep(min(30.0, 5.0 * attempt))
                    continue
                rec.status, rec.failure_code, rec.failure_reason = (
                    JobStatus.NEEDS_ATTENTION, "rate_limited", str(exc))
                return rec
            except APIError as exc:
                if getattr(exc, "retryable", False) and attempt <= self.config.max_job_retries:
                    self.log(f"retry  {asset.id} after {type(exc).__name__} (attempt {attempt})")
                    continue
                rec.status, rec.failure_code, rec.failure_reason = (
                    JobStatus.FAILED, "api_error", str(exc))
                return rec
            rec.generation_id = gen.id
            rec.request_id = getattr(gen, "request_id", None)

            try:
                terminal = poll_until_terminal(self.image_client.get_generation, gen.id, self.config.image_poll)
            except PollTimeout as exc:
                rec.status, rec.failure_code, rec.failure_reason = JobStatus.FAILED, "poll_timeout", str(exc)
                return rec

            if terminal.state is State.COMPLETED:
                dest = Path(self.config.output_dir) / asset.group / f"{asset.id}{ext}"
                try:
                    path = download_output(self.fetch_image, terminal, dest, refresh=self.image_client.get_generation)
                except Exception as exc:
                    rec.status, rec.failure_code, rec.failure_reason = JobStatus.FAILED, "download_failed", str(exc)
                    return rec
                rec.status = JobStatus.COMPLETED
                rec.output_path = str(path)
                return rec

            code = terminal.failure_code
            rec.failure_code, rec.failure_reason = code, terminal.failure_reason
            gf = GenerationFailed(code, terminal.failure_reason, generation_id=gen.id)
            if gf.retryable and attempt <= self.config.max_job_retries:
                self.log(f"retry  {asset.id} after {code} (attempt {attempt})")
                continue
            rec.status = (JobStatus.NEEDS_ATTENTION
                          if code in {"content_moderated", "budget_exhausted"} else JobStatus.FAILED)
            return rec

        rec.status = JobStatus.FAILED  # pragma: no cover
        return rec
