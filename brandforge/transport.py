"""Transport abstraction.

The client talks to a :class:`Transport`, not to ``httpx`` directly. That single
seam is what lets us run the entire pipeline against a fake server in tests and
in ``--mock`` mode with zero network and zero API spend, while production uses
the real HTTPS transport. ``httpx`` is imported lazily so the package (and CI)
works without it installed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Protocol


@dataclass
class Response:
    status_code: int
    headers: Dict[str, str] = field(default_factory=dict)
    json_body: Optional[Dict[str, Any]] = None
    content: bytes = b""

    def header(self, name: str) -> Optional[str]:
        # HTTP headers are case-insensitive; normalize on lookup.
        lower = name.lower()
        for k, v in self.headers.items():
            if k.lower() == lower:
                return v
        return None


class Transport(Protocol):
    def request(
        self,
        method: str,
        path: str,
        *,
        json: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Response:
        ...

    def get_bytes(self, url: str) -> bytes:
        """Download a (presigned) URL. Used for fetching completed outputs."""
        ...


class HttpxTransport:
    """Real transport backed by ``httpx``. Imported lazily."""

    def __init__(self, base_url: str, timeout: float = 30.0) -> None:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - depends on env
            raise RuntimeError(
                "httpx is required for live API calls. Install it with "
                "`pip install httpx`, or use mock mode (--mock) to run without it."
            ) from exc
        self._httpx = httpx
        self._client = httpx.Client(base_url=base_url, timeout=timeout)

    def request(
        self,
        method: str,
        path: str,
        *,
        json: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Response:
        try:
            resp = self._client.request(method, path, json=json, headers=headers)
        except self._httpx.TimeoutException as exc:
            # Surface as a retryable 503-like condition.
            return Response(status_code=599, headers={}, json_body={"detail": f"timeout: {exc}"})
        body: Optional[Dict[str, Any]] = None
        ctype = resp.headers.get("content-type", "")
        if "application/json" in ctype:
            try:
                body = resp.json()
            except ValueError:
                body = None
        return Response(
            status_code=resp.status_code,
            headers=dict(resp.headers),
            json_body=body,
            content=resp.content,
        )

    def get_bytes(self, url: str) -> bytes:
        resp = self._client.get(url)
        resp.raise_for_status()
        return resp.content

    def close(self) -> None:  # pragma: no cover - cleanup
        self._client.close()
