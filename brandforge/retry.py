"""Retry policy for transient failures.

Scope is deliberately narrow: retry only what the docs say is retryable —
``429`` (respecting ``Retry-After``) and ``5xx``. Auth, validation, moderation,
and insufficient-credit errors are *not* retried, because retrying them changes
nothing and wastes time/money.

For polling, the FAQ is explicit that exponential backoff "doesn't buy you
anything" — uni-1 jobs take 30-60s, so a fixed cadence is correct. We therefore
keep backoff for *submission* retries (where it matters) and use a flat interval
in the poller.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Callable, TypeVar

from .errors import APIError, RateLimitError

T = TypeVar("T")


@dataclass
class RetryPolicy:
    max_attempts: int = 4
    base_delay: float = 1.0
    max_delay: float = 30.0
    jitter: float = 0.25  # +/- fraction applied to each delay

    def backoff(self, attempt: int) -> float:
        """Exponential backoff with full-ish jitter for attempt N (1-indexed)."""
        raw = min(self.base_delay * (2 ** (attempt - 1)), self.max_delay)
        spread = raw * self.jitter
        return max(0.0, raw + random.uniform(-spread, spread))


def call_with_retry(
    fn: Callable[[], T],
    policy: RetryPolicy | None = None,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Call ``fn``, retrying retryable :class:`APIError`s per ``policy``.

    Honours ``Retry-After`` on rate-limit errors over computed backoff, since
    the server told us exactly how long to wait.
    """
    policy = policy or RetryPolicy()
    last_exc: Exception | None = None
    for attempt in range(1, policy.max_attempts + 1):
        try:
            return fn()
        except APIError as exc:
            last_exc = exc
            if not getattr(exc, "retryable", False) or attempt == policy.max_attempts:
                raise
            delay = policy.backoff(attempt)
            if isinstance(exc, RateLimitError) and exc.retry_after is not None:
                delay = max(delay, exc.retry_after)
            sleep(delay)
    assert last_exc is not None  # pragma: no cover
    raise last_exc
