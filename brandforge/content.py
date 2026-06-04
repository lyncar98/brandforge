"""Text/content generation on Anthropic's Claude — the second asset modality.

A brand isn't only images. Taglines, the one-line manifesto, social copy, meta
descriptions, and image alt text are *brand assets* too, and they drift exactly
the same way images do when every team writes their own. So BrandForge treats
copy as a first-class, declarative asset: a ``kind: "content"`` entry in the
brandspec, generated from a brief in the brand's voice, version-controlled, and
regenerated through the same plan/apply/state machinery as the imagery.

This module exposes a small, synchronous client surface:

* :class:`ContentRequest`  — a validated request (content_type, brief, limits).
* :class:`ContentResult`   — the generated text plus token usage.
* :class:`ClaudeContentClient` — real backend on the official ``anthropic`` SDK.
* :class:`MockContentClient`   — in-process fake for ``--mock`` / tests / CI.

Unlike image generation there's no polling: Claude returns the text in one call.
Errors are mapped onto the same :mod:`brandforge.errors` hierarchy so retries,
moderation handling, and the manifest behave identically across modalities.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .errors import (
    APIError,
    AuthError,
    GenerationFailed,
    InvalidRequestError,
    RateLimitError,
    ServerError,
)

DEFAULT_MODEL = "claude-sonnet-4-6"
PROMPT_MAX_LEN = 8000


@dataclass
class ContentRequest:
    """A single piece of brand copy to generate."""

    brief: str
    content_type: str = "copy"          # tagline | manifesto | social_post | meta_description | alt_text | ...
    voice: str = ""                     # brand voice language, shared across the spec
    brand_name: str = ""
    model: str = DEFAULT_MODEL
    max_words: Optional[int] = None     # soft length budget surfaced to the model
    max_tokens: int = 1024              # hard output cap
    temperature: float = 0.7

    def validate(self) -> "ContentRequest":
        n = len(self.brief)
        if not (1 <= n <= PROMPT_MAX_LEN):
            raise InvalidRequestError(f"brief must be 1-{PROMPT_MAX_LEN} characters (got {n})")
        if self.max_words is not None and self.max_words <= 0:
            raise InvalidRequestError("max_words must be positive when set")
        if not (0.0 <= self.temperature <= 1.0):
            raise InvalidRequestError("temperature must be between 0.0 and 1.0")
        return self

    def system_prompt(self) -> str:
        bits = [
            f"You are the brand copywriter for {self.brand_name or 'this brand'}.",
            f"Write a single piece of brand copy of type: {self.content_type}.",
        ]
        if self.voice:
            bits.append(f"Brand voice and rules: {self.voice}")
        if self.max_words:
            bits.append(f"Hard limit: at most {self.max_words} words.")
        bits.append(
            "Output ONLY the final copy. No preamble, no explanation, no surrounding "
            "quotes, no markdown headers, no options list. Just the copy itself."
        )
        return " ".join(bits)


@dataclass
class ContentResult:
    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    request_id: Optional[str] = None

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class ClaudeContentClient:
    """Real content backend on the official ``anthropic`` SDK."""

    label = "anthropic / Claude (messages API)"

    def __init__(self, api_key: str, *, timeout: float = 60.0) -> None:
        if not api_key:
            raise AuthError("Missing ANTHROPIC_API_KEY.")
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover
            raise APIError("anthropic is not installed. `pip install anthropic`.") from exc
        # max_retries=0: our retry.call_with_retry is the retry layer, not the SDK's.
        self._anthropic = anthropic
        self._client = anthropic.Anthropic(api_key=api_key.strip(), max_retries=0, timeout=timeout)

    def create_content(self, request: ContentRequest) -> ContentResult:
        request.validate()
        try:
            msg = self._client.messages.create(
                model=request.model,
                max_tokens=request.max_tokens,
                temperature=request.temperature,
                system=request.system_prompt(),
                messages=[{"role": "user", "content": request.brief}],
            )
        except Exception as exc:
            raise self._map_exc(exc) from exc

        text = "".join(
            block.text for block in msg.content if getattr(block, "type", None) == "text"
        ).strip()
        if not text:
            # Treated like an async generation failure so the runner can retry it.
            raise GenerationFailed("generation_failed", "model returned no text", generation_id=msg.id)
        usage = getattr(msg, "usage", None)
        return ContentResult(
            text=text,
            model=msg.model,
            input_tokens=getattr(usage, "input_tokens", 0) if usage else 0,
            output_tokens=getattr(usage, "output_tokens", 0) if usage else 0,
            request_id=getattr(msg, "id", None),
        )

    def _map_exc(self, exc: Exception) -> APIError:
        a = self._anthropic
        msg = str(exc)
        if isinstance(exc, a.RateLimitError):
            return RateLimitError(msg, status_code=429)
        if isinstance(exc, (a.AuthenticationError, a.PermissionDeniedError)):
            return AuthError(msg, status_code=getattr(exc, "status_code", 401))
        if isinstance(exc, a.BadRequestError):
            return InvalidRequestError(msg, status_code=400)
        if isinstance(exc, a.InternalServerError):
            return ServerError(msg, status_code=getattr(exc, "status_code", 500))
        if isinstance(exc, a.APIConnectionError):
            return ServerError(f"connection error: {msg}", status_code=503)
        if isinstance(exc, a.APIStatusError):
            return APIError(msg, status_code=getattr(exc, "status_code", 0))
        return ServerError(msg, status_code=503)


class MockContentClient:
    """Deterministic, in-process fake of the content backend.

    Mirrors the image mock's trigger phrases so the demo and tests can exercise
    failure handling without a key or spend:

        "moderate me"  -> terminal content_moderated failure
        "flake once"   -> fails once (generation_failed), succeeds on retry
    """

    label = "mock content client (no key, no spend)"

    def __init__(self) -> None:
        self.submit_count = 0
        self._flake_budget: dict[str, int] = {}

    def create_content(self, request: ContentRequest) -> ContentResult:
        request.validate()
        self.submit_count += 1
        brief = request.brief.lower()

        if "moderate me" in brief:
            raise GenerationFailed("content_moderated", "Brief flagged by content moderation.")
        if "flake once" in brief:
            budget = self._flake_budget.setdefault(brief, 1)
            if budget > 0:
                self._flake_budget[brief] = budget - 1
                raise GenerationFailed("generation_failed", "Transient model error.")

        text = self._fake_copy(request)
        # Rough, deterministic token accounting so cost reporting has something to show.
        in_tok = max(1, len(request.system_prompt()) + len(request.brief)) // 4
        out_tok = max(1, len(text)) // 4
        return ContentResult(
            text=text, model=request.model, input_tokens=in_tok, output_tokens=out_tok,
            request_id="mock-" + str(self.submit_count),
        )

    @staticmethod
    def _fake_copy(request: ContentRequest) -> str:
        brand = request.brand_name or "Brand"
        ct = request.content_type
        if ct == "tagline":
            return f"{brand}: capability you can prove."
        if ct == "manifesto":
            return (
                f"{brand} exists for the work that fails expensively. We build the "
                f"control plane underneath sensitive AI, so the evidence is a byproduct "
                f"of engineering — not a binder assembled the night before the audit."
            )
        if ct in ("social_post", "social"):
            return (
                f"Governance you generate, not maintain. {brand} turns every eval, "
                f"approval, and release into a node in one evidence graph. #AIgovernance"
            )
        if ct == "meta_description":
            return (
                f"{brand} is an AI governance control plane for sensitive, regulated "
                f"domains — evidence generated as a byproduct of building and running AI."
            )
        if ct == "alt_text":
            return f"Abstract monochrome {brand} brand mark: layered planes and a graph of connected nodes."
        return f"[{ct}] {brand} — {request.brief[:80]}"
