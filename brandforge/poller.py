"""Poll a generation to a terminal state.

Implements the cadence the docs recommend: a short initial wait (the first poll
won't be ready), a flat ~2-3s interval (backoff buys nothing for 30-60s jobs),
and a hard timeout so a stalled job never hangs a worker.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from .errors import PollTimeout
from .models import Generation, State


@dataclass
class PollConfig:
    initial_delay: float = 3.0
    interval: float = 2.5
    timeout: float = 120.0  # ~2 min, per the "standard generation" guidance


def poll_until_terminal(
    get: Callable[[str], Generation],
    generation_id: str,
    config: PollConfig | None = None,
    *,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    on_poll: Callable[[Generation], None] | None = None,
) -> Generation:
    """Poll ``get(generation_id)`` until COMPLETED/FAILED or the deadline.

    Returns the terminal :class:`Generation` (including FAILED — the caller
    decides whether a FAILED state is retryable via its ``failure_code``).
    """
    config = config or PollConfig()
    deadline = clock() + config.timeout
    if config.initial_delay > 0:
        sleep(config.initial_delay)

    while True:
        gen = get(generation_id)
        if on_poll is not None:
            on_poll(gen)
        if gen.state.is_terminal:
            return gen
        if clock() >= deadline:
            raise PollTimeout(generation_id, config.timeout)
        sleep(config.interval)
