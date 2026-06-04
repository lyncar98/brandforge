"""Exception hierarchy for the Luma Agents API client.

The API surfaces failures in two places, and a robust client has to handle both:

* **Synchronous** errors come straight back from ``POST /v1/generations`` (or the
  GET) as an HTTP status with a ``detail`` string. These map to subclasses of
  :class:`APIError` below.
* **Asynchronous** failures happen *after* a job is accepted and only show up
  when polling: ``state`` becomes ``failed`` with a machine-readable
  ``failure_code``. Those map to :class:`GenerationFailed`.

Each error knows whether it is *retryable*, so the retry policy and pipeline can
branch on behaviour rather than on string matching.
"""

from __future__ import annotations

from typing import Optional


class BrandForgeError(Exception):
    """Base class for everything this package raises."""


class APIError(BrandForgeError):
    """A synchronous error returned by the HTTP API."""

    retryable: bool = False

    def __init__(
        self,
        message: str,
        *,
        status_code: Optional[int] = None,
        detail: Optional[str] = None,
        request_id: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.detail = detail
        self.request_id = request_id

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        base = super().__str__()
        bits = []
        if self.status_code is not None:
            bits.append(f"status={self.status_code}")
        if self.request_id:
            bits.append(f"request_id={self.request_id}")
        return f"{base} ({', '.join(bits)})" if bits else base


class AuthError(APIError):
    """401/403 — missing, invalid, revoked, or expired API key. Not retryable."""


class InvalidRequestError(APIError):
    """400/422 — the request body violated a documented constraint. Not retryable."""


class InsufficientCreditsError(APIError):
    """402 — out of credits at submit time. The request was *not* queued."""


class NotFoundError(APIError):
    """404 — generation not found (wrong client, bad id, or non-UUID)."""


class RateLimitError(APIError):
    """429 — RPM or concurrent-job ceiling hit. Retryable after ``retry_after``.

    ``kind`` distinguishes the two cases the docs call out, since they want
    different handling: ``"rpm"`` means wait out the window, ``"concurrent"``
    means wait for an in-flight job to terminate.
    """

    retryable = True

    def __init__(
        self,
        message: str,
        *,
        retry_after: Optional[float] = None,
        kind: str = "rpm",
        **kwargs: object,
    ) -> None:
        super().__init__(message, **kwargs)  # type: ignore[arg-type]
        self.retry_after = retry_after
        self.kind = kind


class ServerError(APIError):
    """5xx — transient server-side failure. Retryable."""

    retryable = True


# Async failure codes from the docs, with their documented "what to do" semantics.
# ``content_moderated`` is deliberately *not* retryable: the decision is
# deterministic, so re-submitting the same prompt just burns latency.
RETRYABLE_FAILURE_CODES = frozenset({"generation_failed", "output_not_found"})
TERMINAL_FAILURE_CODES = frozenset({"content_moderated", "budget_exhausted", "invalid_request"})


class GenerationFailed(BrandForgeError):
    """An asynchronous failure surfaced while polling a generation."""

    def __init__(
        self,
        failure_code: Optional[str],
        failure_reason: Optional[str] = None,
        *,
        generation_id: Optional[str] = None,
    ) -> None:
        super().__init__(failure_reason or failure_code or "generation failed")
        self.failure_code = failure_code
        self.failure_reason = failure_reason
        self.generation_id = generation_id

    @property
    def retryable(self) -> bool:
        return self.failure_code in RETRYABLE_FAILURE_CODES


class PollTimeout(BrandForgeError):
    """A generation did not reach a terminal state before the deadline."""

    def __init__(self, generation_id: str, waited_seconds: float) -> None:
        super().__init__(
            f"generation {generation_id} did not finish within {waited_seconds:.0f}s"
        )
        self.generation_id = generation_id
        self.waited_seconds = waited_seconds
