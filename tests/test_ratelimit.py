import pytest

from brandforge.ratelimit import ConcurrencyLimiter, RpmLimiter


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_rpm_allows_up_to_limit_then_blocks():
    clock = FakeClock()
    lim = RpmLimiter(3, clock=clock)
    assert lim.try_acquire()
    assert lim.try_acquire()
    assert lim.try_acquire()
    assert not lim.try_acquire()  # 4th in the same window is blocked


def test_rpm_window_eviction():
    clock = FakeClock()
    lim = RpmLimiter(2, window=60.0, clock=clock)
    assert lim.try_acquire()
    assert lim.try_acquire()
    assert not lim.try_acquire()
    clock.t = 61.0  # both earlier events age out
    assert lim.try_acquire()


def test_rpm_acquire_sleeps_until_slot_frees():
    clock = FakeClock()
    lim = RpmLimiter(1, window=60.0, clock=clock)
    slept = []

    def fake_sleep(d):
        slept.append(d)
        clock.t += d  # advance time so the next loop iteration succeeds

    lim.acquire(sleep=fake_sleep)  # first one is free
    lim.acquire(sleep=fake_sleep)  # second must wait ~60s
    assert slept and abs(slept[0] - 60.0) < 1e-6


def test_concurrency_limiter_blocks_and_releases():
    lim = ConcurrencyLimiter(1)
    assert lim.acquire(timeout=0.01)
    assert not lim.acquire(timeout=0.01)
    lim.release()
    assert lim.acquire(timeout=0.01)


def test_invalid_limits():
    with pytest.raises(ValueError):
        RpmLimiter(0)
    with pytest.raises(ValueError):
        ConcurrencyLimiter(0)
