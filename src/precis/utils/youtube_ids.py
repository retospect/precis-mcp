"""YouTube video-id extraction + language-list parsing.

Shared between ``handlers/youtube`` (the interactive get path) and
``workers/job_types/youtube_bulk`` (the paced bulk-fetch job, gr531240)
— the workers layer may not import ``precis.handlers``, so the two
helpers live here in utils where both reach them.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

from precis.errors import BadInput

VIDEO_ID_RE = re.compile(r"^[\w-]{11}$")


def extract_video_id(video: str) -> str:
    """Extract an 11-character YouTube video ID from URL or bare id.

    Accepts:
        - ``https://www.youtube.com/watch?v=ID``
        - ``https://youtu.be/ID``
        - ``https://www.youtube.com/shorts/ID``
        - ``https://www.youtube.com/embed/ID``
        - ``https://www.youtube.com/live/ID``
        - bare 11-char ID (e.g. ``79-bApI3GIU``)

    Raises :class:`BadInput` when the id can't be determined.
    """
    video = video.strip()
    if VIDEO_ID_RE.match(video):
        return video

    parsed = urlparse(video)
    host = (parsed.hostname or "").lower().removeprefix("www.")

    if host in ("youtube.com", "m.youtube.com"):
        if parsed.path == "/watch":
            ids = parse_qs(parsed.query).get("v")
            if ids:
                return ids[0]
        for prefix in ("/shorts/", "/embed/", "/live/"):
            if parsed.path.startswith(prefix):
                segment = parsed.path[len(prefix) :].split("/")[0]
                if segment:
                    return segment
    elif host == "youtu.be":
        segment = parsed.path.lstrip("/").split("/")[0]
        if segment:
            return segment

    raise BadInput(
        f"cannot extract YouTube video id from: {video!r}",
        next="get(kind='youtube', id='dQw4w9WgXcQ')  # 11-char id or URL",
    )


def parse_languages(raw: str) -> list[str]:
    """Parse a comma-separated language list, defaulting to ['en']."""
    raw = raw.strip()
    if not raw:
        return ["en"]
    langs = [c.strip() for c in raw.split(",") if c.strip()]
    return langs or ["en"]
