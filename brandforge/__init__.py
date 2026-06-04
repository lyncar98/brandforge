"""BrandForge — manage a brand's creative assets as code.

A brand's identity lives in a single declarative **brandspec** checked into
version control. Images are generated on Luma's ``uni-1`` image API; copy is
generated on Anthropic's Claude. The same Brand-as-Code workflow — ``plan`` to
diff the spec against state, ``apply`` to generate what's missing — drives both
modalities, with the operational concerns a real workflow needs: rate limiting,
retries, graceful failure handling, resumable runs, and a cost/audit report.
"""

from __future__ import annotations

from .client import LumaAgentsClient
from .content import (
    ClaudeContentClient,
    ContentRequest,
    ContentResult,
    MockContentClient,
)
from .errors import (
    APIError,
    AuthError,
    BrandForgeError,
    GenerationFailed,
    InsufficientCreditsError,
    InvalidRequestError,
    PollTimeout,
    RateLimitError,
)
from .kit import BrandKit, KitAsset, KitConfig, KitRunner, PlannedAction, load_kit
from .manifest import JobRecord, JobStatus, Manifest
from .models import Generation, GenerationRequest, ImageRef, Model, State, Style
from .poller import PollConfig
from .retry import RetryPolicy

__version__ = "0.2.0"

__all__ = [
    "__version__",
    "LumaAgentsClient",
    "ClaudeContentClient",
    "MockContentClient",
    "ContentRequest",
    "ContentResult",
    "KitRunner",
    "KitConfig",
    "BrandKit",
    "KitAsset",
    "PlannedAction",
    "load_kit",
    "PollConfig",
    "RetryPolicy",
    "Manifest",
    "JobRecord",
    "JobStatus",
    "Generation",
    "GenerationRequest",
    "ImageRef",
    "Model",
    "State",
    "Style",
    "BrandForgeError",
    "APIError",
    "AuthError",
    "InvalidRequestError",
    "InsufficientCreditsError",
    "RateLimitError",
    "GenerationFailed",
    "PollTimeout",
]
