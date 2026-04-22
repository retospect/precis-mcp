"""YouTubeHandler — fetch YouTube video transcripts (Phase 4).

Ported from ``tubescribe-mcp``.  Thin shim around the
``youtube-transcript-api`` pip package.  No API key required.

Gating:

- ``youtube-transcript-api`` must be importable (part of the
  ``[external]`` extra).  Registry registration is skipped at startup if
  the package is missing.
- No env vars required; ``requires=[]`` on the ``KindSpec``.

Dispatch:

- ``read(path=<video_id_or_url>)`` returns the transcript.
- ``view='languages'`` lists available transcript languages for the
  given video id.
- Selector / depth / page / summarize are ignored — a transcript is an
  atomic string.  ``query`` is a prefix-matched language code list
  (``en``, ``en,es``) — falls back to ``en`` when empty.

Back-compat: the existing ``tubescribe-mcp`` standalone server keeps
working; precis exposes the same functionality under the ``youtube:``
scheme so agents that use the unified precis tool bundle don't need
two MCPs.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

from precis.protocol import ErrorCode, Handler, PrecisError

if TYPE_CHECKING:  # pragma: no cover — type-only
    from youtube_transcript_api import YouTubeTranscriptApi  # noqa: F401

log = logging.getLogger(__name__)


_VIDEO_ID_RE = re.compile(r"^[\w-]{11}$")


# Transcript attribution footer.  The youtube-transcript-api package
# does not return owner/channel info, so we cite the video URL itself
# and warn the user to verify against the source.
_YT_ATTRIBUTION = (
    "---\n"
    "_Source: YouTube video [{video_id}](https://www.youtube.com/watch?v={video_id}). "
    "Transcript © the video's uploader or YouTube (auto-generated).  Cite the "
    "original video, not this transcript; verify quotes against the source._"
)


def _attribution(video_id: str) -> str:
    """Build the YouTube source-attribution footer."""
    return _YT_ATTRIBUTION.format(video_id=video_id)


class YouTubeHandler(Handler):
    """Handler for the ``youtube:`` scheme — transcript fetch.

    Agent usage::

        get(id='youtube:79-bApI3GIU')
        get(type='youtube', id='https://youtu.be/79-bApI3GIU')
        get(id='youtube:79-bApI3GIU/languages')
        get(id='youtube:79-bApI3GIU', grep='en,es')   # preferred langs
    """

    scheme = "youtube"
    writable = False
    views = {"languages", "transcript"}  # default view is full transcript

    def __init__(self) -> None:
        self._api = None  # lazy

    # ---- Client init (lazy) -----------------------------------------

    def _get_api(self):
        if self._api is not None:
            return self._api
        try:
            from youtube_transcript_api import YouTubeTranscriptApi
        except ImportError as exc:
            raise PrecisError(
                ErrorCode.KIND_UNAVAILABLE,
                "youtube-transcript-api package not installed. "
                "Install with: pip install precis-mcp[external]",
            ) from exc
        self._api = YouTubeTranscriptApi()
        return self._api

    # ---- Core read --------------------------------------------------

    def read(
        self,
        path: str,
        selector: str | None,
        view: str | None,
        subview: str | None,
        query: str,
        summarize: bool,
        depth: int,
        page: int,
    ) -> str:
        if not path.strip():
            raise PrecisError(
                ErrorCode.PARAM_INVALID,
                "empty video id",
            )
        try:
            video_id = _extract_video_id(path)
        except ValueError as exc:
            raise PrecisError(
                ErrorCode.ID_MALFORMED,
                str(exc),
            ) from exc

        api = self._get_api()

        if view == "languages":
            return self._list_languages(api, video_id)

        languages = _parse_languages(query)
        return self._fetch_transcript(api, video_id, languages)

    # ---- Helpers ----------------------------------------------------

    def _list_languages(self, api, video_id: str) -> str:
        try:
            transcript_list = api.list(video_id)
        except Exception as exc:
            raise PrecisError(
                ErrorCode.UPSTREAM_ERROR,
                f"YouTube API error: {exc}",
            ) from exc
        lines = [f"Available transcripts for {video_id}:"]
        for t in transcript_list:
            mark = "auto" if t.is_generated else "human"
            lines.append(f"  {t.language_code:<6} {t.language:<30} [{mark}]")
        return "\n".join(lines) + "\n\n" + _attribution(video_id)

    def _fetch_transcript(self, api, video_id: str, languages: list[str]) -> str:
        from youtube_transcript_api._errors import (
            NoTranscriptFound,
            TranscriptsDisabled,
            VideoUnavailable,
        )

        try:
            snippets = api.fetch(video_id, languages=languages)
        except TranscriptsDisabled as exc:
            raise PrecisError(
                ErrorCode.UNAVAILABLE,
                f"Transcripts are disabled for video {video_id}.",
            ) from exc
        except NoTranscriptFound as exc:
            raise PrecisError(
                ErrorCode.ID_NOT_FOUND,
                f"No transcript for {video_id} in languages {languages}. "
                "Try /languages to see what's available.",
            ) from exc
        except VideoUnavailable as exc:
            raise PrecisError(
                ErrorCode.ID_NOT_FOUND,
                f"Video {video_id} is unavailable.",
            ) from exc
        except Exception as exc:
            raise PrecisError(
                ErrorCode.UPSTREAM_ERROR,
                f"YouTube API error: {exc}",
            ) from exc

        body = "\n".join(s.text for s in snippets)
        return f"{body}\n\n{_attribution(video_id)}"


# ---------------------------------------------------------------------------
# Helpers — ported from tubescribe-mcp/src/tubescribe_mcp/transcript.py
# ---------------------------------------------------------------------------


def _extract_video_id(video: str) -> str:
    """Extract an 11-character YouTube video ID from URL or bare id.

    Accepts the same forms as ``tubescribe_mcp.transcript.extract_video_id``:

    - ``https://www.youtube.com/watch?v=ID``
    - ``https://youtu.be/ID``
    - ``https://www.youtube.com/shorts/ID``
    - ``https://www.youtube.com/embed/ID``
    - ``https://www.youtube.com/live/ID``
    - bare 11-char ID (e.g. ``79-bApI3GIU``)

    Raises :class:`ValueError` when the id can't be determined.
    """
    video = video.strip()

    if _VIDEO_ID_RE.match(video):
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

    raise ValueError(f"cannot extract YouTube video id from: {video!r}")


def _parse_languages(query: str) -> list[str]:
    """Parse a comma-separated language-code list, defaulting to ``['en']``.

    Strips whitespace and filters empty entries.  Always returns at
    least one language to satisfy ``youtube-transcript-api``'s API.
    """
    query = query.strip()
    if not query:
        return ["en"]
    langs = [lang.strip() for lang in query.split(",") if lang.strip()]
    return langs or ["en"]
