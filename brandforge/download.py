"""Download completed outputs to local storage.

Presigned output URLs expire after 1 hour, and the docs are explicit: download
promptly to your own storage, don't hand presigned URLs to end users. If the URL
we have has gone stale, we re-poll the generation to mint a fresh one and retry
once.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from .models import Generation, OutputFormat

_EXT = {OutputFormat.PNG: ".png", OutputFormat.JPEG: ".jpg"}


def extension_for(fmt: OutputFormat | None, url: str) -> str:
    if fmt is not None:
        return _EXT[fmt]
    lower = url.lower().split("?", 1)[0]
    if lower.endswith(".png"):
        return ".png"
    if lower.endswith((".jpg", ".jpeg")):
        return ".jpg"
    return ".png"


def download_output(
    fetch: Callable[[str], bytes],
    generation: Generation,
    dest: str | Path,
    *,
    refresh: Callable[[str], Generation] | None = None,
) -> Path:
    """Fetch the first output of a completed generation and write it to ``dest``.

    ``fetch`` downloads bytes for a URL; ``refresh`` re-polls the generation to
    mint a new presigned URL if the first download fails (likely expiry).
    """
    url = generation.first_output_url
    if not url:
        raise ValueError(f"generation {generation.id} has no output URL")

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    try:
        data = fetch(url)
    except Exception:
        if refresh is None:
            raise
        fresh = refresh(generation.id)
        fresh_url = fresh.first_output_url
        if not fresh_url:
            raise
        data = fetch(fresh_url)

    # Write atomically so a crash mid-write never leaves a half-image on disk.
    tmp = dest.with_suffix(dest.suffix + ".part")
    tmp.write_bytes(data)
    tmp.replace(dest)
    return dest
