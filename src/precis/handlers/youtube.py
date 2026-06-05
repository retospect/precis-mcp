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
from contextvars import ContextVar
from typing import Any, ClassVar
from urllib.parse import parse_qs, urlparse

from precis.errors import BadInput, NotFound, Upstream
from precis.handlers._cache_base import CacheBackedHandler, FetchResult
from precis.protocol import KindSpec
from precis.response import Response
from precis.store.types import BlockInsert
from precis.utils.next_block import render_next_section
from precis.utils.optional_deps import require_optional

log = logging.getLogger(__name__)


_VIDEO_ID_RE = re.compile(r"^[\w-]{11}$")


# Per-request language preference, threaded from the public ``get`` to
# ``_canonical_key`` without an instance attribute.
#
# FastMCP runs sync tool callables in a worker-thread pool — concurrent
# calls used to clobber a shared ``self._lang_pref`` and silently mix
# language preferences between cache lookups. ``ContextVar`` gives each
# request (each worker thread, in our case) its own isolated value with
# no cross-thread bleed, and the surrounding ``token`` reset in
# :meth:`YouTubeHandler.get` ensures the variable can't leak past the
# call boundary.
#
# The default is a tuple (immutable) rather than a list — ContextVar
# defaults are shared across contexts that haven't yet called ``set``,
# and a mutable default would let a stray ``.append`` in any caller
# leak across requests. The handler converts to ``list`` at read time
# via the surrounding ``sorted(...)`` call site.
_LANG_PREF: ContextVar[tuple[str, ...]] = ContextVar(
    "precis.youtube._lang_pref", default=("en",)
)


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

    # ── overridden get to honour view='languages' ─────────────────────

    def get(  # type: ignore[override]
        self,
        *,
        id: str | int | None = None,
        q: str | None = None,
        view: str | None = None,
        languages: str | None = None,
        tags: list[str] | None = None,
        untags: list[str] | None = None,
        mode: str | None = None,
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
        # The preference is stashed on a per-request ``ContextVar`` so
        # concurrent worker-thread calls don't clobber each other (the
        # previous instance-attribute approach was racy under FastMCP's
        # thread pool).
        token = _LANG_PREF.set(tuple(_parse_languages(languages or "")))
        try:
            return super().get(
                id=id,
                q=q,
                view=view,
                tags=tags,
                untags=untags,
                mode=mode,
            )
        finally:
            _LANG_PREF.reset(token)

    # ── refresh-by-slug support ───────────────────────────────────────

    def _recover_key(self, ref, cache):  # type: ignore[no-untyped-def]
        """Reconstruct ``<video_id>:<lang_part>`` from cached meta.

        Cache meta stores ``video_id`` and ``languages`` from the
        original fetch; rebuild the canonical key so a slug-only
        refresh (e.g. from the maintenance driver iterating
        ``WATCH:daily`` tags) can re-fetch without the caller having
        to remember the original URL. (gripe:3681 phase 4.)
        """
        meta = cache.meta or {}
        video_id = meta.get("video_id") or ref.slug
        if not video_id:
            return None
        langs = meta.get("languages") or ["en"]
        lang_part = "+".join(sorted(langs))
        return f"{video_id}:{lang_part}"

    # ── canonicalization & cache key ──────────────────────────────────

    def _canonical_key(self, query: str) -> str:
        """Cache key = bare video id + optional language tag.

        Variants like ``https://youtu.be/X``, ``youtube.com/watch?v=X``,
        and bare ``X`` collapse into a single row keyed on the id.
        """
        video_id = _extract_video_id(query)
        # Stable language portion of the key (sorted; empty langs == ['en']).
        lang_part = "+".join(sorted(_LANG_PREF.get()))
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

        yt = require_optional("youtube_transcript_api", extra="external")
        errs = require_optional("youtube_transcript_api._errors", extra="external")
        TranscriptsDisabled = errs.TranscriptsDisabled
        NoTranscriptFound = errs.NoTranscriptFound
        VideoUnavailable = errs.VideoUnavailable

        api = yt.YouTubeTranscriptApi()
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
            raise NotFound(
                f"video {video_id} is unavailable",
                next="verify the URL; the video may be private, deleted, or region-blocked",
            ) from exc
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
    yt = require_optional("youtube_transcript_api", extra="external")
    try:
        transcript_list = yt.YouTubeTranscriptApi().list(video_id)
    except Exception as exc:
        raise Upstream(f"YouTube API error: {exc}") from exc

    lines = [f"Available transcripts for {video_id}:"]
    for t in transcript_list:
        mark = "auto" if t.is_generated else "human"
        lines.append(f"  {t.language_code:<6} {t.language:<30} [{mark}]")
    lines.append("")
    lines.append(f"- {_YT_BASE_ATTRIBUTION}")
    lines.append(f"  Watch: https://www.youtube.com/watch?v={video_id}")
    body = "\n".join(lines)
    # Canonical Next: block — c5 unified-trailer patch. Previously
    # a raw ``Next: get(...)`` f-string that skipped the column
    # alignment every other kind uses.
    body += render_next_section(
        [
            (
                # ``languages=`` is a handler-side kwarg that does NOT
                # appear on the agent-facing MCP get tool signature
                # (``kind/id/view/q/args`` only). The MCP critic flagged
                # the bare ``languages='LANG_CODE'`` form as aspirational
                # 2026-05-02 — copying the hint verbatim hits the MCP
                # boundary's "unknown kwarg" rejection. Use ``args=`` to
                # forward extras to the handler, which is the documented
                # mechanism on every kind that has params beyond the
                # five top-level kwargs.
                f"get(kind='youtube', id='{video_id}', "
                "args={'languages': 'LANG_CODE'})",
                "fetch a specific transcript",
            ),
        ]
    )
    return Response(body=body, cost="[cost: free]")


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
