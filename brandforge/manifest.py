"""Run manifest: the durable record of every job in a pipeline run.

The manifest is what makes a run **resumable and auditable**. Each (product,
channel) pair is one job with a stable key; on a re-run we skip jobs already
marked ``completed`` (with their file still on disk) instead of paying to
regenerate them. It also carries a transparent cost estimate and powers the
human-readable report.

Pricing note: the public docs don't publish the per-image dollar table, so
:data:`PRICE_TABLE` holds clearly-labelled *placeholder estimates* with the
documented *relationships* baked in (uni-1-max ≈ 2.5× uni-1; each reference
image adds a small increment). Override it with real numbers from your Pricing
page when you have them.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# Placeholder estimates. Relationships are real; absolute values are not.
# Images are priced per-image; content is priced per-token (see below).
PRICE_TABLE: Dict[str, float] = {
    "uni-1": 0.02,
    "uni-1-max": 0.05,
}
PRICE_PER_REFERENCE = 0.002

# Content is priced from real token usage. Per-million-token placeholder rates,
# keyed by a substring of the model name; absolute values are illustrative.
CONTENT_PRICE_PER_MTOK = {
    "opus": (15.0, 75.0),    # (input, output) $/1M tokens
    "sonnet": (3.0, 15.0),
    "haiku": (0.8, 4.0),
}
_DEFAULT_CONTENT_RATE = (3.0, 15.0)


def _content_rate(model: str) -> tuple[float, float]:
    for key, rate in CONTENT_PRICE_PER_MTOK.items():
        if key in model:
            return rate
    return _DEFAULT_CONTENT_RATE


class JobStatus:
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"
    NEEDS_ATTENTION = "needs_attention"  # terminal failure a human must look at (e.g. moderated)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def job_key(product_id: str, channel: str) -> str:
    return f"{product_id}::{channel}"


@dataclass
class JobRecord:
    key: str
    product_id: str
    channel: str
    prompt: str
    model: str
    aspect_ratio: Optional[str] = None
    num_refs: int = 0
    status: str = JobStatus.PENDING
    generation_id: Optional[str] = None
    output_path: Optional[str] = None
    failure_code: Optional[str] = None
    failure_reason: Optional[str] = None
    attempts: int = 0
    request_id: Optional[str] = None
    input_tokens: int = 0
    output_tokens: int = 0
    updated_at: str = field(default_factory=_now)

    def estimated_cost(self) -> float:
        """Estimated charge for this job. Only completed jobs are counted."""
        if self.status != JobStatus.COMPLETED:
            return 0.0
        # Content (token-billed) when we have a token count or a Claude model.
        if self.input_tokens or self.output_tokens or self.model.startswith("claude"):
            in_rate, out_rate = _content_rate(self.model)
            return round(
                (self.input_tokens / 1_000_000) * in_rate
                + (self.output_tokens / 1_000_000) * out_rate,
                6,
            )
        base = PRICE_TABLE.get(self.model, PRICE_TABLE["uni-1"])
        return round(base + self.num_refs * PRICE_PER_REFERENCE, 6)

    def touch(self) -> None:
        self.updated_at = _now()


@dataclass
class Manifest:
    brand_name: str
    output_dir: str
    run_id: str = field(default_factory=lambda: datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S"))
    created_at: str = field(default_factory=_now)
    records: Dict[str, JobRecord] = field(default_factory=dict)

    # -- persistence ----------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        return {
            "brand_name": self.brand_name,
            "output_dir": self.output_dir,
            "run_id": self.run_id,
            "created_at": self.created_at,
            "records": [asdict(r) for r in self.records.values()],
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Manifest":
        m = cls(
            brand_name=d["brand_name"],
            output_dir=d["output_dir"],
            run_id=d.get("run_id", ""),
            created_at=d.get("created_at", _now()),
        )
        for rd in d.get("records", []):
            rec = JobRecord(**rd)
            m.records[rec.key] = rec
        return m

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> "Manifest":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    # -- accessors ------------------------------------------------------------

    def upsert(self, record: JobRecord) -> None:
        record.touch()
        self.records[record.key] = record

    def get(self, key: str) -> Optional[JobRecord]:
        return self.records.get(key)

    def is_satisfied(self, key: str) -> bool:
        """True if this job is already completed and its output file still exists."""
        rec = self.records.get(key)
        if rec is None or rec.status != JobStatus.COMPLETED or not rec.output_path:
            return False
        return Path(rec.output_path).exists()

    # -- reporting ------------------------------------------------------------

    def counts(self) -> Dict[str, int]:
        out = {JobStatus.PENDING: 0, JobStatus.COMPLETED: 0, JobStatus.FAILED: 0, JobStatus.NEEDS_ATTENTION: 0}
        for r in self.records.values():
            out[r.status] = out.get(r.status, 0) + 1
        return out

    def failure_breakdown(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for r in self.records.values():
            if r.failure_code:
                out[r.failure_code] = out.get(r.failure_code, 0) + 1
        return out

    def estimated_cost(self) -> float:
        return round(sum(r.estimated_cost() for r in self.records.values()), 4)

    def render_markdown(self) -> str:
        c = self.counts()
        total = len(self.records)
        done = c[JobStatus.COMPLETED]
        lines = [
            f"# BrandForge run report -- {self.brand_name}",
            "",
            f"- Run ID: `{self.run_id}`",
            f"- Output: `{self.output_dir}`",
            f"- Jobs: **{done}/{total} completed**, "
            f"{c[JobStatus.FAILED]} failed, {c[JobStatus.NEEDS_ATTENTION]} need attention, "
            f"{c[JobStatus.PENDING]} pending",
            f"- Estimated cost: **~${self.estimated_cost():.4f}** "
            f"(placeholder rates -- see Pricing)",
            "",
        ]
        fb = self.failure_breakdown()
        if fb:
            lines.append("## Failure breakdown")
            lines.append("")
            lines.append("| failure_code | count |")
            lines.append("| --- | --- |")
            for code, n in sorted(fb.items()):
                lines.append(f"| `{code}` | {n} |")
            lines.append("")
        lines.append("## Jobs")
        lines.append("")
        lines.append("| product | channel | status | model | output / reason |")
        lines.append("| --- | --- | --- | --- | --- |")
        for r in sorted(self.records.values(), key=lambda x: (x.product_id, x.channel)):
            detail = r.output_path or r.failure_reason or r.failure_code or ""
            lines.append(
                f"| {r.product_id} | {r.channel} | {r.status} | {r.model} | {detail} |"
            )
        return "\n".join(lines) + "\n"
