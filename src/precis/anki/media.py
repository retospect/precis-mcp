"""Fetch a bounded raster attachment once, when its card is authored."""

from __future__ import annotations

import base64
import hashlib
import time
from typing import Any

import httpx

from precis.errors import BadInput, Upstream
from precis.utils.http import http_client
from precis.utils.safe_fetch import DnsTimeout, SsrfBlocked, safe_stream

_MAX_BYTES = 1024 * 1024
_NEXT = "use a direct public HTTPS URL for a PNG or JPEG up to 1 MiB"


def fetch_image(url: Any) -> dict[str, str]:
    """Return a JSON-safe attachment without retaining its source URL."""
    try:
        parsed = httpx.URL(url) if isinstance(url, str) else None
    except httpx.InvalidURL as exc:
        raise BadInput("invalid Anki image URL", next=_NEXT) from exc
    if parsed is None or parsed.scheme != "https" or not parsed.host or parsed.userinfo:
        raise BadInput("expected a public HTTPS image URL", next=_NEXT)

    deadline = time.monotonic() + 15
    data = bytearray()
    try:
        with (
            http_client(timeout=5.0, headers={"Accept-Encoding": "identity"}) as client,
            safe_stream(client, "GET", url) as response,
        ):
            response.raise_for_status()
            if (
                response.headers.get("Content-Encoding", "identity").lower()
                != "identity"
            ):
                raise BadInput("Anki image response uses HTTP compression", next=_NEXT)
            for chunk in response.iter_raw():
                if time.monotonic() > deadline:
                    raise Upstream("Anki image download timed out", next=_NEXT)
                if len(data) + len(chunk) > _MAX_BYTES:
                    raise BadInput("Anki image exceeds 1 MiB", next=_NEXT)
                data.extend(chunk)
            mime = (
                response.headers.get("Content-Type", "").split(";")[0].strip().lower()
            )
    except SsrfBlocked as exc:
        raise BadInput("Anki image URL or redirect was blocked", next=_NEXT) from exc
    except (DnsTimeout, httpx.HTTPError, httpx.InvalidURL) as exc:
        raise Upstream("could not download the Anki image", next=_NEXT) from exc

    if mime == "image/png" and data.startswith(b"\x89PNG\r\n\x1a\n"):
        ext = "png"
    elif mime == "image/jpeg" and data.startswith(b"\xff\xd8\xff"):
        ext = "jpg"
    else:
        raise BadInput("Anki image must be served as a PNG or JPEG", next=_NEXT)
    digest = hashlib.sha256(data).hexdigest()
    return {
        "filename": f"precis-{digest}.{ext}",
        "data": base64.b64encode(data).decode("ascii"),
    }
