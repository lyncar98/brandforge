"""Real image backend on Luma's official ``luma-agents`` SDK.

A drop-in for :class:`brandforge.client.LumaAgentsClient` — same
``create_generation`` / ``get_generation`` / ``get_output_bytes`` surface, same
``models.Generation`` return type — so the pipeline, kit runner, poller, and
manifest use it without changes.

Two deliberate choices:

* We hand the SDK our already-validated ``GenerationRequest.to_body()`` (the SDK's
  ``create`` params mirror the REST body one-to-one), so client-side validation
  still runs first.
* We set ``max_retries=0`` on the SDK and translate its exceptions into *our*
  typed errors, so this project's retry / rate-limit / poll machinery stays the
  thing doing the work — which is the whole point of the toolkit.
"""

from __future__ import annotations

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


def _retry_after(exc) -> Optional[float]:
    resp = getattr(exc, "response", None)
    if resp is None:
        return None
    try:
        return float(resp.headers.get("retry-after"))
    except (TypeError, ValueError):
        return None


class LumaSDKImageClient:
    base_url = "https://agents.lumalabs.ai/v1 (official luma-agents SDK)"

    def __init__(self, auth_token: str, *, timeout: float = 60.0) -> None:
        if not auth_token:
            raise AuthError("Missing API key. Set LUMA_AGENTS_API_KEY.")
        try:
            from luma_agents import Luma
        except ImportError as exc:  # pragma: no cover
            raise APIError("luma-agents is not installed. `pip install luma-agents`.") from exc
        # max_retries=0: our retry.call_with_retry is the retry layer, not the SDK's.
        self._client = Luma(auth_token=auth_token.strip(), max_retries=0, timeout=timeout)

    def create_generation(self, request: GenerationRequest) -> Generation:
        body = request.validate().to_body()
        with self._mapped_errors():
            sdk_gen = self._client.generations.create(**body)
        return Generation.from_dict(sdk_gen.to_dict())

    def get_generation(self, generation_id: str) -> Generation:
        with self._mapped_errors():
            sdk_gen = self._client.generations.get(generation_id)
        return Generation.from_dict(sdk_gen.to_dict())

    def get_output_bytes(self, url: str) -> bytes:
        import httpx

        resp = httpx.get(url, timeout=60.0, follow_redirects=True)
        resp.raise_for_status()
        return resp.content

    # -- error translation ----------------------------------------------------

    def _mapped_errors(self):
        return _SDKErrorMapper()


class _SDKErrorMapper:
    """Context manager that turns luma_agents exceptions into brandforge ones."""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc is None:
            return False
        import luma_agents

        msg = str(exc)
        if isinstance(exc, luma_agents.RateLimitError):
            raise RateLimitError(msg, status_code=429, retry_after=_retry_after(exc)) from exc
        if isinstance(exc, (luma_agents.AuthenticationError, luma_agents.PermissionDeniedError)):
            raise AuthError(msg, status_code=getattr(exc, "status_code", 401)) from exc
        if isinstance(exc, luma_agents.NotFoundError):
            raise NotFoundError(msg, status_code=404) from exc
        if isinstance(exc, (luma_agents.BadRequestError, luma_agents.UnprocessableEntityError)):
            raise InvalidRequestError(msg, status_code=getattr(exc, "status_code", 400)) from exc
        if isinstance(exc, luma_agents.InternalServerError):
            raise ServerError(msg, status_code=getattr(exc, "status_code", 500)) from exc
        if isinstance(exc, luma_agents.APIConnectionError):
            # network/timeout: retryable in our policy
            raise ServerError(f"connection error: {msg}", status_code=503) from exc
        if isinstance(exc, luma_agents.APIStatusError):
            code = getattr(exc, "status_code", 0)
            if code == 402:
                raise InsufficientCreditsError(msg, status_code=402) from exc
            raise APIError(msg, status_code=code) from exc
        return False  # not ours — propagate
