import pytest

from brandforge.errors import AuthError, RateLimitError, ServerError
from brandforge.retry import RetryPolicy, call_with_retry


def test_succeeds_first_try():
    calls = []
    assert call_with_retry(lambda: calls.append(1) or "ok", sleep=lambda d: None) == "ok"
    assert calls == [1]


def test_retries_then_succeeds_on_server_error():
    attempts = {"n": 0}

    def fn():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise ServerError("boom", status_code=500)
        return "ok"

    out = call_with_retry(fn, RetryPolicy(max_attempts=5), sleep=lambda d: None)
    assert out == "ok"
    assert attempts["n"] == 3


def test_does_not_retry_non_retryable():
    attempts = {"n": 0}

    def fn():
        attempts["n"] += 1
        raise AuthError("nope", status_code=401)

    with pytest.raises(AuthError):
        call_with_retry(fn, sleep=lambda d: None)
    assert attempts["n"] == 1


def test_gives_up_after_max_attempts():
    def fn():
        raise ServerError("boom", status_code=500)

    with pytest.raises(ServerError):
        call_with_retry(fn, RetryPolicy(max_attempts=3), sleep=lambda d: None)


def test_rate_limit_honours_retry_after():
    delays = []
    attempts = {"n": 0}

    def fn():
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RateLimitError("slow down", retry_after=42.0)
        return "ok"

    call_with_retry(
        fn,
        RetryPolicy(max_attempts=3, base_delay=1.0, jitter=0.0),
        sleep=delays.append,
    )
    assert delays == [42.0]  # used Retry-After, not the 1s backoff
