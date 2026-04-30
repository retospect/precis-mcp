"""``youtube`` kind — fetch YouTube video transcripts.

Subclasses :class:`CacheBackedHandler`; cache key is the bare 11-char
video ID (extracted from URL forms) so different URL-shapes pointing
at the same video share a row. Free, no API key.

Supports the ``view='languages'`` shortcut to list available
transcript languages without committing to a fetch (useful when the
default ``en`` track doesn't exist).
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import parse_qs, urlparse

from precis.errors import BadInput, NotFound, Upstream
from precis.handlers._cache_base import CacheBackedHandler, FetchResult
from precis.protocol import KindSpec
from precis.response import Response
from precis.store.types import BlockInsert

if TYPE_CHECKING:
    from precis.store import Store

log = logging.getLogger(__name__)


_VIDEO_ID_RE = re.compile(r"^[\w-]{11}$")


_YT_BASE_ATTRIBUTION = (
    "Source: YouTube. Transcript © the video's uploader or YouTube "
    "(auto-generated). Cite the original video, not this transcript; "
    "verify quotes against the source."
)


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class YouTubeHandler(CacheBackedHandler):
    """``youtube`` — transcript fetch via youtube-transcript-api. Free."""

    spec: ClassVar[KindSpec] = KindSpec(
        kind="youtube",
        title="YouTube transcripts",
        description=(
            "Fetch a YouTube video transcript by id or URL. "
            "Use view='languages' to list available transcript languages."
        ),
        supports_get=True,
        is_numeric=False,
        id_required=True,
        views=("languages",),
    )

    provider: ClassVar[str] = "youtube"
    # Transcripts very rarely change after upload — 30-day TTL is a
    # generous balance between freshness and cost (which is zero
    # anyway). The agent can force a refresh by deleting the ref.
    ttl_seconds: ClassVar[int | None] = 30 * 24 * 60 * 60  # 30 days
    attribution: ClassVar[str] = _YT_BASE_ATTRIBUTION
    corpus_slug: ClassVar[str] = "default"
    example_query: ClassVar[str] = "dQw4w9WgXcQ"

    def __init__(self, *, store: Store) -> None:
        super().__init__(store=store)

    # ── overridden get to honour view='languages' ─────────────────────

    def get(  # type: ignore[override]
        self,
        *,
        id: str | int | None = None,
        q: str | None = None,
        view: str | None = None,
        languages: str | None = None,
        **_kw: Any,
    ) -> Response:
        if view == "languages":
            # `languages` view is a side query, not cached. Returns the
            # list of available tracks for the given video so the agent
            # can pick one before paying the (free) transcript fetch.
            video_input = self._coerce_query(id, q)
            video_id = _extract_video_id(video_input)
            return _list_languages(video_id)

        # Plug `languages=` kwarg into the cache key so distinct
        # language preferences cache separately. Default to English.
        # We thread it via the canonical key.
        self._lang_pref = _parse_languages(languages or "")
        return super().get(id=id, q=q, view=view)

    # ── canonicalization & cache key ──────────────────────────────────

    def _canonical_key(self, query: str) -> str:
        """Cache key = bare video id + optional language tag.

        Variants like ``https://youtu.be/X``, ``youtube.com/watch?v=X``,
        and bare ``X`` collapse into a single row keyed on the id.
        """
        video_id = _extract_video_id(query)
        langs = getattr(self, "_lang_pref", ["en"])
        # Stable language portion of the key (sorted; empty langs == ['en']).
        lang_part = "+".join(sorted(langs))
        return f"{video_id}:{lang_part}"

    def _slug_for(self, key: str) -> str:
        """Use the bare video id (sans language suffix) as the slug.

        Different language fetches share a slug; the cache key still
        differs because it includes the language. Last-fetched body
        wins for ref content (acceptable: transcript text is the same
        snippet stream just translated).
        """
        # key is "<video_id>:<lang_part>"; take just the video id for the slug
        return key.split(":", 1)[0]

    # ── upstream call ─────────────────────────────────────────────────

    def _fetch(self, key: str) -> FetchResult:
        video_id, lang_part = key.split(":", 1)
        languages = lang_part.split("+") if lang_part else ["en"]

        try:
            from youtube_transcript_api import YouTubeTranscriptApi
            from youtube_transcript_api._errors import (
                NoTranscriptFound,
                TranscriptsDisabled,
                VideoUnavailable,
            )
        except ImportError as exc:  # pragma: no cover — guarded at registry
            raise Upstream(
                "youtube-transcript-api is not installed",
                next="pip install 'precis-mcp[external]'",
            ) from exc

        api = YouTubeTranscriptApi()
        try:
            snippets = api.fetch(video_id, languages=languages)
        except TranscriptsDisabled as exc:
            raise NotFound(
                f"transcripts are disabled for video {video_id}",
                next=f"get(kind='youtube', id='{video_id}', view='languages')",
            ) from exc
        except NoTranscriptFound as exc:
            raise NotFound(
                f"no transcript for {video_id} in languages={languages}",
                next=f"get(kind='youtube', id='{video_id}', view='languages')",
            ) from exc
        except VideoUnavailable as exc:
            raise NotFound(f"video {video_id} is unavailable") from exc
        except Exception as exc:
            raise Upstream(f"YouTube API error: {exc}") from exc

        text = "\n".join(s.text for s in snippets).strip()
        return FetchResult(
            title=f"YouTube transcript: {video_id}",
            body_blocks=[BlockInsert(pos=0, text=text)],
            cost_usd=None,  # free
            meta={
                "video_id": video_id,
                "languages": languages,
                "snippet_count": len(snippets),
            },
        )

    # ── render: append per-video deep-link below attribution ──────────

    def _render(self, ref, cache, *, hit):  # type: ignore[no-untyped-def]
        resp = super()._render(ref, cache, hit=hit)
        video_id = (cache.meta or {}).get("video_id") or ref.slug
        deep_link = f"  Watch: https://www.youtube.com/watch?v={video_id}"
        return Response(
            body=resp.body + "\n" + deep_link,
            cost=resp.cost,
        )


# ---------------------------------------------------------------------------
# Side queries (not cached — they're cheap metadata)
# ---------------------------------------------------------------------------


def _list_languages(video_id: str) -> Response:
    """Return the available transcript languages for a video.

    Not stored in `cache_state` — it's a metadata query that's small
    and changes if the uploader adds tracks. Each call hits the API
    directly.
    """
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except ImportError as exc:  # pragma: no cover
        raise Upstream(
            "youtube-transcript-api is not installed",
            next="pip install 'precis-mcp[external]'",
        ) from exc

    try:
        transcript_list = YouTubeTranscriptApi().list(video_id)
    except Exception as exc:
        raise Upstream(f"YouTube API error: {exc}") from exc

    lines = [f"Available transcripts for {video_id}:"]
    for t in transcript_list:
        mark = "auto" if t.is_generated else "human"
        lines.append(f"  {t.language_code:<6} {t.language:<30} [{mark}]")
    lines.append("")
    lines.append(f"— {_YT_BASE_ATTRIBUTION}")
    lines.append(f"  Watch: https://www.youtube.com/watch?v={video_id}")
    lines.append("")
    lines.append(f"Next: get(kind='youtube', id='{video_id}', languages='LANG_CODE')")
    return Response(body="\n".join(lines), cost="[cost: free]")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_video_id(video: str) -> str:
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

    raise BadInput(
        f"cannot extract YouTube video id from: {video!r}",
        next="get(kind='youtube', id='dQw4w9WgXcQ')  # 11-char id or URL",
    )


def _parse_languages(raw: str) -> list[str]:
    """Parse a comma-separated language list, defaulting to ['en']."""
    raw = raw.strip()
    if not raw:
        return ["en"]
    langs = [c.strip() for c in raw.split(",") if c.strip()]
    return langs or ["en"]
