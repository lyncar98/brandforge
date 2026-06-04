import pytest

from brandforge.client import LumaAgentsClient
from brandforge.errors import (
    APIError,
    AuthError,
    InsufficientCreditsError,
    InvalidRequestError,
    NotFoundError,
    RateLimitError,
    ServerError,
)
from brandforge.models import GenerationRequest, State
from brandforge.transport import Response


class StubTransport:
    def __init__(self, response: Response):
        self.response = response
        self.calls = []

    def request(self, method, path, *, json=None, headers=None):
        self.calls.append((method, path, json, headers))
        return self.response

    def get_bytes(self, url):
        return b"bytes"


def make_client(response: Response) -> LumaAgentsClient:
    return LumaAgentsClient("luma-api-test", transport=StubTransport(response))


def test_requires_prefix():
    with pytest.raises(AuthError):
        LumaAgentsClient("nope-123", transport=StubTransport(Response(200)))


def test_key_is_trimmed():
    c = LumaAgentsClient("  luma-api-abc\n", transport=StubTransport(Response(200)))
    assert c.api_key == "luma-api-abc"


def test_create_parses_generation_and_request_id():
    resp = Response(
        201,
        headers={"X-Request-Id": "req-1", "X-RateLimit-Limit": "30", "X-RateLimit-Remaining": "29"},
        json_body={"id": "g1", "state": "queued", "model": "uni-1", "type": "image"},
    )
    c = make_client(resp)
    gen = c.create_generation(GenerationRequest(prompt="hello"))
    assert gen.id == "g1"
    assert gen.state is State.QUEUED
    assert gen.request_id == "req-1"
    assert c.last_rate_limit.limit == 30
    assert c.last_rate_limit.remaining == 29


@pytest.mark.parametrize(
    "status,exc",
    [
        (401, AuthError),
        (403, AuthError),
        (402, InsufficientCreditsError),
        (404, NotFoundError),
        (400, InvalidRequestError),
        (422, InvalidRequestError),
        (500, ServerError),
        (503, ServerError),
        (418, APIError),
    ],
)
def test_error_mapping(status, exc):
    c = make_client(Response(status, json_body={"detail": "boom"}))
    with pytest.raises(exc):
        c.get_generation("g1")


def test_rate_limit_kind_and_retry_after():
    c = make_client(
        Response(429, headers={"Retry-After": "12"}, json_body={"detail": "Too many concurrent jobs"})
    )
    with pytest.raises(RateLimitError) as ei:
        c.get_generation("g1")
    assert ei.value.kind == "concurrent"
    assert ei.value.retry_after == 12.0


def test_rate_limit_rpm_kind():
    c = make_client(Response(429, json_body={"detail": "Rate limit exceeded"}))
    with pytest.raises(RateLimitError) as ei:
        c.get_generation("g1")
    assert ei.value.kind == "rpm"


def test_validation_runs_before_transport():
    transport = StubTransport(Response(201, json_body={"id": "x", "state": "queued"}))
    c = LumaAgentsClient("luma-api-test", transport=transport)
    with pytest.raises(InvalidRequestError):
        c.create_generation(GenerationRequest(prompt=""))
    assert transport.calls == []  # never hit the wire
