"""Typed models for the Luma Agents ``uni-1`` image API.

These mirror the wire format of ``POST /v1/generations`` and ``GET
/v1/generations/{id}``. The interesting part is :meth:`GenerationRequest.validate`,
which enforces the documented constraints *client-side* so we fail fast and
locally instead of spending a round-trip to learn the API rejected us.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from .errors import GenerationFailed, InvalidRequestError


class Model(str, Enum):
    UNI_1 = "uni-1"
    UNI_1_MAX = "uni-1-max"


class GenType(str, Enum):
    IMAGE = "image"
    IMAGE_EDIT = "image_edit"


class Style(str, Enum):
    AUTO = "auto"
    MANGA = "manga"


class OutputFormat(str, Enum):
    PNG = "png"
    JPEG = "jpeg"


class State(str, Enum):
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"

    @property
    def is_terminal(self) -> bool:
        return self in (State.COMPLETED, State.FAILED)


# The nine documented aspect ratios. ``manga`` is restricted to the portrait subset.
ASPECT_RATIOS = ("3:1", "2:1", "16:9", "3:2", "1:1", "2:3", "9:16", "1:2", "1:3")
PORTRAIT_ASPECT_RATIOS = ("2:3", "9:16", "1:2", "1:3")

PROMPT_MAX_LEN = 6000
PROMPT_MIN_LEN = 1
MAX_IMAGE_REFS_IMAGE = 9
MAX_IMAGE_REFS_EDIT = 8


@dataclass
class ImageRef:
    """A reference image: exactly one of ``url`` or ``data`` (+ ``media_type``)."""

    url: Optional[str] = None
    data: Optional[str] = None  # base64-encoded
    media_type: Optional[str] = None

    def to_body(self) -> Dict[str, Any]:
        if self.url is not None:
            return {"url": self.url}
        return {"data": self.data, "media_type": self.media_type}

    def validate(self, label: str) -> None:
        has_url = self.url is not None
        has_data = self.data is not None
        if has_url and has_data:
            raise InvalidRequestError(f"{label}: provide either 'url' or 'data', not both")
        if not has_url and not has_data:
            raise InvalidRequestError(f"{label}: provide either 'url' or 'data', not neither")
        if has_data and not self.media_type:
            raise InvalidRequestError(f"{label}: 'media_type' is required with 'data'")


@dataclass
class GenerationRequest:
    """A single image generation/edit request."""

    prompt: str
    type: GenType = GenType.IMAGE
    model: Model = Model.UNI_1
    aspect_ratio: Optional[str] = None
    style: Style = Style.AUTO
    output_format: Optional[OutputFormat] = None
    web_search: bool = False
    image_ref: List[ImageRef] = field(default_factory=list)
    source: Optional[ImageRef] = None

    def validate(self) -> "GenerationRequest":
        """Raise :class:`InvalidRequestError` if the request breaks a documented rule.

        Mirrors the server-side 400/422 cases so callers fail fast and locally.
        """
        n = len(self.prompt)
        if not (PROMPT_MIN_LEN <= n <= PROMPT_MAX_LEN):
            raise InvalidRequestError(
                f"prompt must be {PROMPT_MIN_LEN}-{PROMPT_MAX_LEN} characters (got {n})"
            )

        if self.aspect_ratio is not None and self.aspect_ratio not in ASPECT_RATIOS:
            raise InvalidRequestError(
                f"aspect_ratio={self.aspect_ratio!r} is not one of {', '.join(ASPECT_RATIOS)}"
            )

        is_edit = self.type is GenType.IMAGE_EDIT
        if is_edit and self.source is None:
            raise InvalidRequestError("source is required when type='image_edit'")
        if not is_edit and self.source is not None:
            raise InvalidRequestError("source is only valid when type='image_edit'")

        max_refs = MAX_IMAGE_REFS_EDIT if is_edit else MAX_IMAGE_REFS_IMAGE
        if len(self.image_ref) > max_refs:
            raise InvalidRequestError(
                f"too many image_ref entries: {len(self.image_ref)} > {max_refs} for type={self.type.value}"
            )
        for i, ref in enumerate(self.image_ref):
            ref.validate(f"image_ref[{i}]")
        if self.source is not None:
            self.source.validate("source")

        # manga is portrait-only on image generation (ignored on edits).
        if self.style is Style.MANGA and not is_edit:
            if self.aspect_ratio is not None and self.aspect_ratio not in PORTRAIT_ASPECT_RATIOS:
                raise InvalidRequestError(
                    f"aspect_ratio={self.aspect_ratio!r} is not allowed when style='manga'. "
                    f"Valid aspect_ratio: {', '.join(PORTRAIT_ASPECT_RATIOS)}"
                )
        return self

    def to_body(self) -> Dict[str, Any]:
        """Serialize to the JSON body, omitting unset optionals."""
        body: Dict[str, Any] = {"prompt": self.prompt}
        if self.type is not GenType.IMAGE:
            body["type"] = self.type.value
        if self.model is not Model.UNI_1:
            body["model"] = self.model.value
        if self.aspect_ratio is not None:
            body["aspect_ratio"] = self.aspect_ratio
        if self.style is not Style.AUTO:
            body["style"] = self.style.value
        if self.output_format is not None:
            body["output_format"] = self.output_format.value
        if self.web_search:
            body["web_search"] = True
        if self.image_ref:
            body["image_ref"] = [r.to_body() for r in self.image_ref]
        if self.source is not None:
            body["source"] = self.source.to_body()
        return body


@dataclass
class GenerationOutput:
    type: str
    url: str

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "GenerationOutput":
        return cls(type=d.get("type", "image"), url=d["url"])


@dataclass
class Generation:
    """Status + output of a generation, as returned by create/get."""

    id: str
    state: State
    model: Optional[Model] = None
    type: Optional[GenType] = None
    created_at: Optional[str] = None
    failure_code: Optional[str] = None
    failure_reason: Optional[str] = None
    output: List[GenerationOutput] = field(default_factory=list)
    request_id: Optional[str] = None

    @classmethod
    def from_dict(cls, d: Dict[str, Any], *, request_id: Optional[str] = None) -> "Generation":
        return cls(
            id=d["id"],
            state=State(d["state"]),
            model=Model(d["model"]) if d.get("model") else None,
            type=GenType(d["type"]) if d.get("type") else None,
            created_at=d.get("created_at"),
            failure_code=d.get("failure_code"),
            failure_reason=d.get("failure_reason"),
            output=[GenerationOutput.from_dict(o) for o in (d.get("output") or [])],
            request_id=request_id,
        )

    @property
    def first_output_url(self) -> Optional[str]:
        return self.output[0].url if self.output else None

    def raise_if_failed(self) -> "Generation":
        if self.state is State.FAILED:
            raise GenerationFailed(
                self.failure_code, self.failure_reason, generation_id=self.id
            )
        return self
