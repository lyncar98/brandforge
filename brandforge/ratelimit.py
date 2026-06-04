"""Client-side rate limiting.

The API enforces two independent limits per client (not per key):

* **RPM** on ``POST /v1/generations`` over a rolling 60s window.
* **Concurrent jobs** — the number of active (non-terminal) generations.

We mirror both on our side so we *stay under* the ceiling instead of relying on
429s to tell us we crossed it. Staying under is cheaper (no wasted round-trips)
and friendlier to a shared client quota.

Both primitives are thread-safe because the pipeline submits jobs from a thread
pool.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Callable, Deque


class RpmLimiter:
    """Sliding-window limiter: at most ``max_per_minute`` acquisitions per 60s.

    Matches the API's rolling-window semantics — requests age out individually
    after the window, there is no fixed reset tick.
    """

    def __init__(self, max_per_minute: int, *, window: float = 60.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        if max_per_minute <= 0:
            raise ValueError("max_per_minute must be positive")
        self.max_per_minute = max_per_minute
        self.window = window
        self._clock = clock
        self._events: Deque[float] = deque()
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)

    def _evict(self, now: float) -> None:
        cutoff = now - self.window
        while self._events and self._events[0] <= cutoff:
            self._events.popleft()

    def acquire(self, *, sleep: Callable[[float], None] = time.sleep) -> None:
        while True:
            with self._cv:
                now = self._clock()
                self._evict(now)
                if len(self._events) < self.max_per_minute:
                    self._events.append(now)
                    return
                wait = self._events[0] + self.window - now
            sleep(max(wait, 0.0))

    def try_acquire(self) -> bool:
        with self._lock:
            now = self._clock()
            self._evict(now)
            if len(self._events) < self.max_per_minute:
                self._events.append(now)
                return True
            return False


class ConcurrencyLimiter:
    """A counting semaphore for the concurrent-job ceiling, usable as a context manager."""

    def __init__(self, max_concurrent: int) -> None:
        if max_concurrent <= 0:
            raise ValueError("max_concurrent must be positive")
        self.max_concurrent = max_concurrent
        self._sem = threading.BoundedSemaphore(max_concurrent)

    def acquire(self, timeout: float | None = None) -> bool:
        return self._sem.acquire(timeout=timeout) if timeout else self._sem.acquire()

    def release(self) -> None:
        self._sem.release()

    def __enter__(self) -> "ConcurrencyLimiter":
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()
