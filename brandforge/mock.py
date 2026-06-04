"""An in-process fake of the Luma Agents API.

This is what makes the project demoable and CI-able without a key or a dollar of
spend. It implements the real wire contract closely enough to exercise every
code path the pipeline cares about:

* ``POST /generations`` returns 201 + ``state=queued`` and rate-limit headers.
* ``GET  /generations/{id}`` advances the job ``queued -> processing -> completed``
  over a few polls and mints a fresh presigned-style URL each time.
* Prompts can be steered to deterministic failures so the demo shows graceful
  handling of moderation, transient errors, and rate limiting.
* ``get_bytes`` returns a tiny valid PNG so downloads write real files.

Trigger phrases (case-insensitive substring of the prompt):
    "moderate me"   -> async failure, failure_code=content_moderated (terminal)
    "flake once"    -> fails once with generation_failed, then succeeds on retry
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from .transport import Response

# A 1x1 transparent PNG — enough to write a real, openable file on disk.
_PNG_1PX = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06"
    b"\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05"
    b"\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


@dataclass
class _Job:
    id: str
    prompt: str
    model: str
    type: str
    polls: int = 0
    # Async failure code this generation will report once it finishes processing,
    # or None to complete successfully. Decided at submit time.
    async_fail_code: Optional[str] = None


@dataclass
class MockConfig:
    polls_until_complete: int = 2  # GETs before a job reports completed
    rpm_limit: int = 30


class MockTransport:
    """Deterministic, thread-safe fake transport implementing the Transport protocol."""

    def __init__(self, config: Optional[MockConfig] = None) -> None:
        self.config = config or MockConfig()
        self._jobs: Dict[str, _Job] = {}
        self._lock = threading.Lock()
        self.submit_count = 0
        # Per-prompt flake budget: a "flake once" prompt fails its first
        # generation, then succeeds on the resubmitted (retried) one.
        self._flake_budget: Dict[str, int] = {}

    def request(
        self,
        method: str,
        path: str,
        *,
        json: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Response:
        if method == "POST" and path == "/generations":
            return self._create(json or {})
        if method == "GET" and path.startswith("/generations/"):
            return self._get(path.rsplit("/", 1)[-1])
        return Response(status_code=404, json_body={"detail": "Not found"})

    def _rate_headers(self) -> Dict[str, str]:
        remaining = max(self.config.rpm_limit - self.submit_count, 0)
        return {
            "Content-Type": "application/json",
            "X-API-Version": "2026-04-01",
            "X-Request-Id": uuid.uuid4().hex,
            "X-RateLimit-Limit": str(self.config.rpm_limit),
            "X-RateLimit-Remaining": str(remaining),
            "X-RateLimit-Reset": "0",
        }

    def _create(self, body: Dict[str, Any]) -> Response:
        prompt = (body.get("prompt") or "").lower()
        with self._lock:
            self.submit_count += 1
            async_fail_code: Optional[str] = None
            if "moderate me" in prompt:
                async_fail_code = "content_moderated"
            elif "flake once" in prompt:
                budget = self._flake_budget.setdefault(prompt, 1)
                if budget > 0:
                    self._flake_budget[prompt] = budget - 1
                    async_fail_code = "generation_failed"
            job = _Job(
                id=str(uuid.uuid4()),
                prompt=prompt,
                model=body.get("model", "uni-1"),
                type=body.get("type", "image"),
                async_fail_code=async_fail_code,
            )
            self._jobs[job.id] = job
        return Response(
            status_code=201,
            headers=self._rate_headers(),
            json_body={
                "id": job.id,
                "state": "queued",
                "model": job.model,
                "type": job.type,
                "created_at": "2026-06-02T00:00:00.000Z",
            },
        )

    def _get(self, generation_id: str) -> Response:
        with self._lock:
            job = self._jobs.get(generation_id)
            if job is None:
                return Response(status_code=404, json_body={"detail": "Generation not found"})
            job.polls += 1
            polls = job.polls

        base = {
            "id": job.id,
            "model": job.model,
            "type": job.type,
            "created_at": "2026-06-02T00:00:00.000Z",
        }
        headers = {"X-Request-Id": uuid.uuid4().hex, "Content-Type": "application/json"}

        if polls < self.config.polls_until_complete:
            return Response(status_code=200, headers=headers, json_body={**base, "state": "processing"})

        if job.async_fail_code is not None:
            reasons = {
                "content_moderated": "Input or output flagged by content moderation.",
                "generation_failed": "Transient internal model error.",
            }
            return Response(
                status_code=200,
                headers=headers,
                json_body={
                    **base,
                    "state": "failed",
                    "failure_code": job.async_fail_code,
                    "failure_reason": reasons.get(job.async_fail_code, "Generation failed."),
                },
            )

        # Fresh presigned-style URL on each poll, mirroring the real 1hr-expiry behaviour.
        url = f"https://mock.lumalabs.local/output/{job.id}/{uuid.uuid4().hex}.png"
        return Response(
            status_code=200,
            headers=headers,
            json_body={**base, "state": "completed", "output": [{"type": "image", "url": url}]},
        )

    def get_bytes(self, url: str) -> bytes:
        return _PNG_1PX
