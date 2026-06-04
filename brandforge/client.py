"""Typed client for the Luma Agents ``uni-1`` image API.

Wraps the two endpoints:

* ``POST /v1/generations`` -> submit a job (returns 201 + job id, ``state=queued``)
* ``GET  /v1/generations/{id}`` -> poll status / mint a fresh presigned output URL

Responsibilities that belong *here* (not in the pipeline):

* attach Bearer auth and a per-request ``X-Request-Id`` for traceability
* translate HTTP status codes + ``detail`` strings into the typed exceptions in
  :mod:`brandforge.errors`
* parse the ``X-RateLimit-*`` / ``Retry-After`` headers so callers can pace
  themselves and distinguish RPM from concurrent-job limiting
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Optional

from .errors import (
    APIError,
    AuthError,
    InsufficientCreditsError,
    InvalidRequestError,
    NotFoundError,
    RateLimitError,
    ServerError,
)
from .models import Generation, GenerationRequest
from .transport import HttpxTransport, Response, Transport

DEFAULT_BASE_URL = "https://agents.lumalabs.ai/v1"
API_KEY_PREFIX = "luma-api-"


@dataclass
class RateLimitSnapshot:
    """Last-seen rate-limit headers from a successful POST."""

    limit: Optional[int] = None
    remaining: Optional[int] = None
    reset: Optional[int] = None


def _to_int(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _to_float(value: Optional[str]) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def map_http_error(resp: Response) -> APIError:
    """Translate an HTTP error response from the Agents API into a typed exception."""
    detail = resp.json_body.get("detail") if resp.json_body else None
    request_id = resp.header("X-Request-Id")
    status = resp.status_code
    kwargs = dict(status_code=status, detail=detail, request_id=request_id)
    msg = detail or f"HTTP {status}"

    if status in (401, 403):
        return AuthError(msg, **kwargs)
    if status == 402:
        return InsufficientCreditsError(msg, **kwargs)
    if status == 404:
        return NotFoundError(msg, **kwargs)
    if status in (400, 422):
        return InvalidRequestError(msg, **kwargs)
    if status == 429:
        retry_after = _to_float(resp.header("Retry-After"))
        kind = "concurrent" if detail and "concurrent" in detail.lower() else "rpm"
        return RateLimitError(msg, retry_after=retry_after, kind=kind, **kwargs)
    if status >= 500:
        return ServerError(msg, **kwargs)
    return APIError(msg, **kwargs)


class LumaAgentsClient:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        transport: Optional[Transport] = None,
        timeout: float = 30.0,
    ) -> None:
        if not api_key:
            raise AuthError("Missing API key. Set LUMA_AGENTS_API_KEY.")
        # Trim is intentional: pasted keys frequently carry trailing newlines.
        self.api_key = api_key.strip()
        if not self.api_key.startswith(API_KEY_PREFIX):
            raise AuthError(
                f"API key does not look like a Luma key (expected prefix {API_KEY_PREFIX!r})."
            )
        self.base_url = base_url
        self.transport: Transport = transport or HttpxTransport(base_url, timeout=timeout)
        self.last_rate_limit = RateLimitSnapshot()

    # -- public API -----------------------------------------------------------

    def create_generation(self, request: GenerationRequest) -> Generation:
        request.validate()
        resp = self._request("POST", "/generations", json=request.to_body())
        self._capture_rate_limit(resp)
        data = resp.json_body or {}
        return Generation.from_dict(data, request_id=resp.header("X-Request-Id"))

    def get_generation(self, generation_id: str) -> Generation:
        resp = self._request("GET", f"/generations/{generation_id}")
        data = resp.json_body or {}
        return Generation.from_dict(data, request_id=resp.header("X-Request-Id"))

    def get_output_bytes(self, url: str) -> bytes:
        return self.transport.get_bytes(url)

    # -- internals ------------------------------------------------------------

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            # Client-generated id is echoed back; lets us correlate our logs with theirs.
            "X-Request-Id": uuid.uuid4().hex,
        }

    def _request(self, method: str, path: str, *, json: Optional[dict] = None) -> Response:
        resp = self.transport.request(method, path, json=json, headers=self._headers())
        if resp.status_code >= 400:
            raise map_http_error(resp)
        return resp

    def _capture_rate_limit(self, resp: Response) -> None:
        self.last_rate_limit = RateLimitSnapshot(
            limit=_to_int(resp.header("X-RateLimit-Limit")),
            remaining=_to_int(resp.header("X-RateLimit-Remaining")),
            reset=_to_int(resp.header("X-RateLimit-Reset")),
        )
