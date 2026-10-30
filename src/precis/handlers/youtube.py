"""``youtube`` kind — fetch YouTube video transcripts.

Subclasses :class:`CacheBackedHandler`; cache key is the bare 11-char
video ID (extracted from URL forms) so different URL-shapes pointing
at the same video share a row. Free, no API key.

Supports the ``view='languages'`` shortcut to list available
transcript languages without committing to a fetch (useful when the
default ``en`` track doesn't exist).

Discovery over what's already cached (gr498108): ``id='/recent'``
(or a bare get) pages the cached-video list — video id, title,
channel, duration, cache state — and ``search`` fuses a
title/channel leg with transcript-chunk hits, grouped one row per
video, so a vague topic description finds the video without SQL.

Chunked transcripts (gr540142, gr533135): a transcript lands as
time-windowed chunks (~1–2 kB each, stamped ``start_s``/``end_s``
from the caption track) instead of one unaddressable blob — before
this, any transcript over the 4 kB context cap was cached but
unreadable through the tool. ``view='toc'`` lists the chunks with
sizes and timestamps, and ``id='<video>~A..B'`` reads a range.
Transcripts cached before this change are re-windowed lazily on
first read (timestamps are unrecoverable from the stored blob, so
legacy chunks carry no ``start_s``).
"""

from __future__ import annotations

import contextlib
import logging
import re
from collections.abc import Callable, Iterator
from contextvars import ContextVar
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, ClassVar
from urllib.parse import parse_qs, urlparse

import psycopg

from precis.errors import BadInput, NotFound, RateLimited, Upstream
from precis.handlers._cache_base import (
    CacheBackedHandler,
    FetchResult,
    _parse_chunk_selector,
)
from precis.protocol import KindSpec
from precis.response import Response
from precis.store import SEMANTIC_DISTANCE_FLOOR
from precis.store.types import CacheEntry, ChunkInsert, ChunkRow, Ref
from precis.utils.embed_query import embed_query, query_vec_for
from precis.utils.http import (
    Policy,
    call_with_backoff,
    paced_requests,
    policy_for,
    polite_user_agent,
)
from precis.utils.next_block import render_next_section
from precis.utils.optional_deps import require_optional
from precis.utils.search_header import format_search_headline
from precis.utils.search_merge import SearchHit, block_hits_to_search_hits
from precis.utils.text import excerpt as _excerpt

log = logging.getLogger(__name__)


_VIDEO_ID_RE = re.compile(r"^[\w-]{11}$")

#: Target size of one transcript chunk (~1–2 kB per the read-surface
#: contract — every chunk must fit the 4 kB context cap with room for
#: the header/attribution chrome around it).
_TRANSCRIPT_CHUNK_TARGET = 1500

#: A stored body that is ONE blob bigger than this is a pre-chunking
#: transcript; it is re-windowed on first read. Below this the single
#: chunk is itself a valid ``~0`` read.
_LEGACY_BLOB_MIN = 3072

#: Byte budget for the rows of ``view='toc'`` — the map itself must fit
#: the 4 kB context cap, so rows stop here and a ``rest:`` line names
#: the range the omitted rows point at.
_TOC_ROW_BUDGET = 3000


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


_THROTTLE_RE = re.compile(r"ratelimit|rate limit|\b429\b|too many requests|blocked")


def _upstream_error(exc: Exception, *, default_next: str, retry_call: str) -> Upstream:
    """Map a failed YouTube call to ``Upstream``. A throttle (the limiter's
    ``RateLimited``, an HTTP 429, an IP/request block) carries a retry
    hint with a wait and a pacing rule instead of the generic retry line
    (gr531240)."""
    probe = f"{type(exc).__name__} {exc}".lower()
    if _THROTTLE_RE.search(probe):
        return Upstream(
            f"YouTube API error: {str(exc) or type(exc).__name__} (YouTube is "
            "throttling this server's transcript requests)",
            next=[
                f"wait about 60 s, then retry: {retry_call}",
                "bulk fetches: run them one at a time, a few seconds apart; "
                "parallel callers share one limiter row and trip it together",
            ],
        )
    return Upstream(f"YouTube API error: {exc}", next=default_next)


def _paced_call(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    with _yt_paced():
        return fn(*args, **kwargs)


def _yt_policy(service: str, *, attempts: int = 2, cap: float = 10.0) -> Policy:
    """Bounded interactive policy: a live agent call must not sit in long
    backoff. The scrape gets its own ``service`` so exhausted best-effort
    scrapes cannot open the breaker that guards transcript fetches (both
    share the ``host:www.youtube.com`` limiter row)."""
    return replace(policy_for(service), attempts=attempts, cap=cap, limiter_wait_s=5.0)


#: Limiter-row key source for the transcript client (which owns its own HTTP).
_YT_HOST_URL = "https://www.youtube.com/"
_YT_HOST = "www.youtube.com"


@contextlib.contextmanager
def _yt_paced() -> Iterator[None]:
    """Meter each request the transcript library issues (watch page,
    innertube, caption track) on the ``host:www.youtube.com`` row, and send
    the honest UA in place of the library's bare ``python-requests``."""
    with paced_requests(
        _YT_HOST,
        service="youtube",
        policy=_yt_policy("youtube"),
        user_agent=polite_user_agent(),
    ):
        yield


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
            "Use view='languages' to list available transcript languages, "
            "view='toc' to map a cached transcript's chunks. "
            "id='/recent' lists cached videos; search matches title, "
            "channel and transcript. Long transcripts read back in "
            "chunk ranges: id='<video_id>~N' or id='<video_id>~N..M'."
        ),
        supports_get=True,
        supports_search=True,
        supports_search_hits=True,
        # Long transcripts are unreadable whole — the ``slug~N`` /
        # ``slug~N..M`` range read (base, gr533135) is the targeted read.
        supports_chunk_selectors=True,
        is_numeric=False,
        id_required=True,
        views=("languages", "body", "toc"),
    )

    provider: ClassVar[str] = "youtube"
    # The handler emits its own time-windowed chunks in ``_fetch``
    # (~1–2 kB, timestamped); the base's generic overlap splitter
    # would re-cut them and strand the per-window ``start_s``/``end_s``
    # meta, so it is opted out here.
    chunk_target_chars: ClassVar[int] = 0
    # Transcripts very rarely change after upload — 30-day TTL is a
    # generous balance between freshness and cost (which is zero
    # anyway). The agent can force a refresh by deleting the ref.
    ttl_seconds: ClassVar[int | None] = 30 * 24 * 60 * 60  # 30 days
    attribution: ClassVar[str] = _YT_BASE_ATTRIBUTION
    corpus_slug: ClassVar[str] = "default"
    example_query: ClassVar[str] = "dQw4w9WgXcQ"

    # ── overridden get to honour view='languages' ─────────────────────

    def get(
        self,
        *,
        id: str | int | None = None,
        q: str | None = None,
        view: str | None = None,
        languages: str | None = None,
        tags: list[str] | None = None,
        untags: list[str] | None = None,
        mode: str | None = None,
        page: int | None = None,
        page_size: int | None = None,
        no_fetch: bool = False,
        refresh: bool = False,
        ttl_days: int | None = None,
        literal: bool = False,
        **_kw: Any,
    ) -> Response:
        if view == "languages":
            # `languages` view is a side query, not cached. Returns the
            # list of available tracks for the given video so the agent
            # can pick one before paying the (free) transcript fetch.
            video_input = self._coerce_query(id, q)
            video_id = _extract_video_id(video_input)
            return _list_languages(video_id)

        if view is not None and view not in ("body", "toc"):
            # The eye-ladder labels in view= are intercepted by the
            # dispatcher before this method runs, so any view reaching
            # here other than the base's ``body`` read or ``toc`` is
            # genuinely unknown — refuse, don't silently ignore.
            raise BadInput(
                f"unknown view={view!r} for kind='youtube'",
                options=["languages", "toc", "body"],
                next=(
                    "extent= picks an eye-ladder rung (kwd|summary|verbatim|fisheye|…)"
                ),
            )

        # Cached-video listing (gr498108): bare get / '' / '/' /
        # '/recent' pages the rows this kind has already fetched.
        if self._is_listing_request(id, q):
            return self._render_recent_videos(page=page, page_size=page_size)
        if page is not None or page_size is not None:
            raise BadInput(
                "page=/page_size= page the /recent listing, not a transcript read",
                next="get(kind='youtube', id='/recent', args={'page': 2})",
            )

        # ``view='toc'`` — the chunk map of a cached transcript
        # (gr540142). Read-only, like ``view='body'``: it never fires a
        # provider fetch.
        if view == "toc":
            return self._render_toc(id=id, q=q)

        # Chunk selector: ``id='<video>~N'`` / ``'<video>~A..B'`` — the
        # shape the dispatcher synthesizes for a resolved chunk handle on
        # kinds that mint one (youtube chunks mint no handle; ``<video>~N``
        # IS the address, resolved by the dispatcher's bare-slug kind
        # inference). Intercepted here rather than left to the base's
        # selector branch so a URL prefix resolves to the bare-video-id
        # slug via ``_extract_video_id``.
        if isinstance(id, str) and "~" in id:
            parsed = _parse_chunk_selector(id.strip())
            if parsed is not None:
                prefix, lo, hi = parsed
                return self._render_chunk_selector(
                    self._selector_slug(prefix),
                    lo,
                    hi,
                    tags=tags,
                    untags=untags,
                    view=view,
                )

        # Plug `languages=` kwarg into the cache key so distinct
        # language preferences cache separately. Default to English.
        # The preference is stashed on a per-request ``ContextVar`` so
        # concurrent worker-thread calls don't clobber each other (the
        # previous instance-attribute approach was racy under FastMCP's
        # thread pool).
        token = _LANG_PREF.set(tuple(_parse_languages(languages or "")))
        try:
            # ``no_fetch`` / ``refresh`` / ``ttl_days`` / ``literal`` are
            # re-declared on this signature so the dispatch extras gate
            # (which reads the most-derived ``get``) forwards them to the
            # base instead of refusing them — ``no_fetch`` is what the
            # /recent trailer advertises.
            return super().get(
                id=id,
                q=q,
                view=view,
                tags=tags,
                untags=untags,
                mode=mode,
                no_fetch=no_fetch,
                refresh=refresh,
                ttl_days=ttl_days,
                literal=literal,
                **_kw,
            )
        finally:
            _LANG_PREF.reset(token)

    # ── /recent listing — one row per cached video ────────────────────

    def _render_recent_videos(
        self,
        *,
        page: int | None = None,
        page_size: int | None = None,
    ) -> Response:
        """Paginated cached-video listing (gr498108).

        One row per video: the video-id slug (its pastable handle on
        this kind — youtube carries no universal handle), title,
        channel, duration and cache state, newest fetch first.
        ``page=``/``page_size=`` ride ``args=`` on the MCP surface.
        """
        try:
            page = max(1, int(page or 1))
        except (TypeError, ValueError):
            raise BadInput(
                f"page must be a positive integer, got {page!r}",
                next="get(kind='youtube', id='/recent')",
            ) from None
        try:
            limit = int(page_size if page_size is not None else 20)
        except (TypeError, ValueError):
            raise BadInput(
                f"page_size must be a positive integer ≤ 100, got {page_size!r}",
                next="get(kind='youtube', id='/recent', args={'page_size': 20})",
            ) from None
        if not 1 <= limit <= 100:
            raise BadInput(
                f"page_size must be a positive integer ≤ 100, got {limit!r}",
                next="get(kind='youtube', id='/recent', args={'page_size': 20})",
            )

        entries = self.store.list_cache_entries(
            kind=self.spec.kind,
            provider=self.provider,
            limit=limit,
            offset=(page - 1) * limit,
        )
        total = self.store.count_cache_entries(
            kind=self.spec.kind, provider=self.provider
        )
        heading = "# cached youtube videos"
        if not entries:
            if page > 1 and total:
                pages = -(-total // limit)
                body = (
                    f"{heading}\n\npage {page}: no entries — {total} "
                    f"cached in {pages} page(s)\n"
                )
            else:
                body = f"{heading}\n\n_(no cached youtube videos yet.)_\n"
            body += render_next_section(
                [
                    (
                        "get(kind='youtube', id='<video id or URL>')",
                        "fetch and cache a transcript",
                    ),
                ]
            )
            return Response(body=body)

        pages = -(-total // limit)
        lines: list[str] = [heading, ""]
        for ref, cache in entries:
            bits = _video_listing_bits(cache)
            title = ref.title or "(untitled)"
            if len(title) > 80:
                title = title[:77] + "..."
            lines.append(f"- `{ref.slug or ref.id}` — {title}  _{' · '.join(bits)}_")
        lines.append("")
        first = (page - 1) * limit + 1
        last = first + len(entries) - 1
        lines.append(f"_showing {first}–{last} of {total} (page {page} of {pages})._")
        calls: list[tuple[str, str]] = [
            (
                "get(kind='youtube', id='<video_id>', args={'no_fetch': True})",
                "read a cached transcript — never fetches",
            ),
            (
                "search(kind='youtube', q='<title, channel or topic>')",
                "find a cached video by title, channel or transcript",
            ),
        ]
        if page < pages:
            calls.append(
                (
                    f"get(kind='youtube', id='/recent', args={{'page': {page + 1}}})",
                    f"see page {page + 1} of {pages}",
                )
            )
        body = "\n".join(lines) + render_next_section(calls)
        return Response(body=body)

    # ── search — title/channel leg + per-video grouped chunks ─────────

    def search(
        self,
        *,
        q: str | None = None,
        page_size: int = 10,
        page: int = 1,
        mode: str | None = None,
        per_paper: int | None = None,
        **_kw: Any,
    ) -> Response:
        """Find a cached video — hits grouped one row per video.

        Two legs (gr498108): a title/channel match over the cache rows
        themselves (the "which cached video did I mean" signal a bare
        chunk search lacks), then transcript-chunk hits capped at
        ``per_paper`` (default 1) chunks per video so one long talk
        can't flood the page. ``page=`` pages the merged video list.
        """
        if q is None or not q.strip():
            raise BadInput(
                "search requires q= for kind='youtube'",
                next="search(kind='youtube', q='<title, channel or topic>')",
            )
        q = q.strip()
        if not isinstance(page, int) or page < 1:
            page = 1
        try:
            page_size = int(page_size)
        except (TypeError, ValueError):
            raise BadInput(
                f"page_size must be a positive integer ≤ 100, got {page_size!r}",
                next="search(kind='youtube', q='…', page_size=10)",
            ) from None
        if not 1 <= page_size <= 100:
            raise BadInput(
                f"page_size must be a positive integer ≤ 100, got {page_size!r}",
                next="search(kind='youtube', q='…', page_size=10)",
            )
        cap = 1
        if per_paper is not None:
            if (
                isinstance(per_paper, bool)
                or not isinstance(per_paper, int)
                or per_paper < 1
            ):
                raise BadInput(
                    f"per_paper must be a positive integer, got {per_paper!r}",
                    next="per_paper=2 keeps at most 2 transcript hits per video",
                )
            cap = per_paper

        query_vec = query_vec_for(self.embedder, q, mode)

        # Title/channel leg — the cache-row match chunk search can't
        # express (a video whose *title* names the topic but whose
        # transcript never does). Fetch a generous pool through the
        # requested page: the merge below pages over the union, so the
        # leg must reach past ``page * page_size`` rows or later pages
        # lose title matches *and* the "of K" total undercounts.
        title_pool = min(max(50, page * page_size), 500)
        title_hits = self.store.search_cache_entry_titles(
            kind=self.spec.kind,
            q=q,
            meta_fields=("channel_name",),
            limit=title_pool,
        )

        # Transcript-chunk leg, capped per video in the fused pool.
        pool = min(max(50, page * page_size * cap), 500)
        counts: dict[int, int] = {}
        chunk_hits = self.store.chunks.search_chunks_multi(
            q_texts=[q],
            query_vecs=[query_vec] if query_vec else [],
            mode=mode,
            kind=self.spec.kind,
            limit=pool,
            max_distance=SEMANTIC_DISTANCE_FLOOR,
            per_paper=cap,
            ref_counts_out=counts,
        )

        # Group chunk hits per ref (fused order = best chunk first).
        chunks_by_ref: dict[int, list[tuple[Any, Ref, float]]] = {}
        ref_order: list[int] = []
        ref_by_id: dict[int, Ref] = {}
        for block, ref, score in chunk_hits:
            if ref.id not in chunks_by_ref:
                chunks_by_ref[ref.id] = []
                ref_order.append(ref.id)
                ref_by_id[ref.id] = ref
            chunks_by_ref[ref.id].append((block, ref, score))

        # Merge: title/channel matches first (they answer "which video"),
        # then chunk-hit videos in fused order. Page over the union.
        merged: list[tuple[Ref, CacheEntry | None, str]] = []
        seen: set[int] = set()
        for ref, tcache in title_hits:
            seen.add(ref.id)
            merged.append((ref, tcache, "title/channel match"))
        for ref_id in ref_order:
            if ref_id in seen:
                continue
            merged.append((ref_by_id[ref_id], None, "transcript match"))
        total_found = len(merged)
        rows = merged[(page - 1) * page_size : page * page_size]
        pool_saturated = len(chunk_hits) >= pool or len(title_hits) >= title_pool

        if not rows:
            return self._search_empty(q, page=page, total=total_found)

        lines = [
            format_search_headline(
                n_returned=len(rows),
                total=None if pool_saturated else total_found,
                noun="video hit",
                query=q,
            )
        ]
        shown_blocks: list[int] = []
        for ref, cache, why in rows:
            slug = ref.slug or str(ref.id)
            if cache is None:
                # Chunk-leg row — the title leg didn't carry the
                # cache_state row. One PK lookup for the channel /
                # duration / freshness tail.
                found = self.store.get_cache_entry_by_ref_id(
                    kind=self.spec.kind, ref_id=ref.id
                )
                cache = found[1] if found else None
            meta = (cache.meta if cache else {}) or {}
            group = chunks_by_ref.get(ref.id) or []
            if group:
                score = group[0][2]
                head = f"## `{slug}` — {ref.title}  (score={score:.4f})"
            else:
                head = f"## `{slug}` — {ref.title}  _({why})_"
            lines.append("")
            lines.append(head)
            bits = _video_listing_bits(cache) if cache else []
            if bits:
                lines.append(f"_{' · '.join(bits)}_")
            for block, _ref, _score in group[:cap]:
                shown_blocks.append(block.id)
                lines.append(_excerpt(block.text))
            if not group:
                desc = (meta.get("description") or "").strip()
                if desc:
                    lines.append(_excerpt(desc))
            more = counts.get(ref.id, len(group)) - len(group)
            if more > 0:
                lines.append(
                    f"_(+{more} more transcript hit{'s' if more > 1 else ''} "
                    "in this video)_"
                )
        if shown_blocks:
            self.store.chunks.bump_salience(shown_blocks)
        calls: list[tuple[str, str]] = [
            (
                "get(kind='youtube', id='<video_id>', args={'no_fetch': True})",
                "read a cached transcript — never fetches",
            ),
        ]
        if page * page_size < total_found:
            calls.append(
                (
                    f"search(kind='youtube', q={q!r}, page={page + 1})",
                    f"page {page + 1} of {-(-total_found // page_size)}",
                )
            )
        body = "\n".join(lines) + render_next_section(calls)
        return Response(body=body)

    def _search_empty(self, q: str, *, page: int, total: int) -> Response:
        """The no-hits render: closest cached titles, then the way out."""
        if page > 1 and total:
            return Response(body=f"no more video hits for {q!r} (page {page})")
        body = f"no cached youtube videos match {q!r}"
        close = self.store.search_cache_entry_titles(
            kind=self.spec.kind,
            q=q,
            meta_fields=("channel_name",),
            min_similarity=0.15,
            limit=5,
        )
        if close:
            body += "\n\nclosest cached titles:"
            for ref, cache in close:
                channel = (cache.meta or {}).get("channel_name")
                suffix = f" ({channel})" if channel else ""
                body += f"\n- `{ref.slug or ref.id}` — {ref.title}{suffix}"
        body += render_next_section(
            [
                (
                    "get(kind='youtube', id='/recent')",
                    "list every cached video",
                ),
                (
                    "get(kind='youtube', id='<video id or URL>')",
                    "fetch and cache a transcript",
                ),
            ]
        )
        return Response(body=body)

    def search_hits(self, **kw: Any) -> list[SearchHit]:
        """Chunk-level hits for cross-kind fan-out — one row per video.

        ``ref_level_dedupe`` collapses every chunk of a video onto the
        ``youtube:<slug>`` key so a long transcript can't fill a merged
        page with duplicate rows of itself (gr498108). The ``**kw``
        shape matches ``Handler.search_hits`` so no override
        suppression is needed (the ratchet is a floor, not a budget).
        """
        q = kw.get("q") or ""
        mode = kw.get("mode")
        query_vec = kw.get("query_vec")
        page_size = kw.get("page_size") or 10
        if not (q and q.strip()):
            return []
        if (mode or "").strip().lower() == "lexical":
            query_vec = None
        elif query_vec is None:
            query_vec = embed_query(self.embedder, q)
        triples = self.store.chunks.search_chunks(
            q=q,
            query_vec=query_vec,
            mode=mode,
            kind=self.spec.kind,
            limit=page_size,
            max_distance=SEMANTIC_DISTANCE_FLOOR,
        )
        self.store.chunks.bump_salience([block.id for block, _r, _s in triples])
        return block_hits_to_search_hits(
            triples, kind=self.spec.kind, ref_level_dedupe=True
        )

    # ── refresh-by-slug support ───────────────────────────────────────

    def _recover_key(self, ref, cache):
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

    def _canonical_key(self, query: str, *, literal: bool = False) -> str:
        """Cache key = bare video id + optional language tag.

        Variants like ``https://youtu.be/X``, ``youtube.com/watch?v=X``,
        and bare ``X`` collapse into a single row keyed on the id.
        Free text isn't an id — the caller wanted a title/topic lookup
        (gr498108), so the refusal points at search and the /recent
        list rather than at a bare-id example.
        """
        try:
            video_id = _extract_video_id(query)
        except BadInput as exc:
            if "~" in query:
                # A ``~`` tail that isn't a valid selector (``~abc``,
                # ``~1..``): name the real grammar rather than leaving
                # the caller at "cannot extract video id".
                raise BadInput(
                    exc.cause,
                    next=(
                        "get(kind='youtube', id='<video_id>~A..B') reads a "
                        "chunk range; view='toc' lists the chunks"
                    ),
                ) from exc
            if any(c.isspace() for c in query.strip()):
                safe_q = " ".join(query.split())
                search_hint = (
                    f"search(kind='youtube', q={safe_q!r})"
                    if "'" not in safe_q
                    else "search(kind='youtube', q='<topic>')"
                )
                raise BadInput(
                    exc.cause,
                    next=[
                        f"{search_hint} — find a cached video by "
                        "title, channel or transcript",
                        "get(kind='youtube', id='/recent') — list cached videos",
                    ],
                ) from exc
            raise
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

        yt = require_optional("youtube_transcript_api")
        errs = require_optional("youtube_transcript_api._errors")
        TranscriptsDisabled = errs.TranscriptsDisabled
        NoTranscriptFound = errs.NoTranscriptFound
        VideoUnavailable = errs.VideoUnavailable
        # The client's own rate-limit surface — YouTube blocking this
        # host's IP for too many requests (gr531240). Looked up by name
        # so a stubbed ``_errors`` module without them stays importable.
        blocked_errors = tuple(
            cls
            for name in ("RequestBlocked", "IpBlocked")
            if isinstance(cls := getattr(errs, name, None), type)
            and issubclass(cls, BaseException)
        )
        from precis.utils.rate_limit import RateLimited as _LimiterRefused

        api = yt.YouTubeTranscriptApi()
        try:
            # Politeness core (host row host:www.youtube.com, 1/s): limiter
            # token per attempt; retries only transport errors / HTTP 429/5xx
            # surfaced by the client. Domain errors (disabled, no transcript,
            # unavailable) are not retryable and propagate unchanged.
            snippets = call_with_backoff(
                lambda: _paced_call(api.fetch, video_id, languages=languages),
                service="youtube",
                policy=_yt_policy("youtube"),
                url=_YT_HOST_URL,
            )
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
        except blocked_errors as exc:
            # YouTube blocked this host's IP (RequestBlocked/IpBlocked —
            # the client's "too many requests" surface). The client
            # exposes no Retry-After; a conservative wait goes in the
            # next= unless the exception carries one (gr531240).
            hinted = getattr(exc, "retry_after_s", None) or getattr(
                exc, "retry_after", None
            )
            when = (
                f"~{hinted:g} s"
                if isinstance(hinted, (int, float))
                else "a few minutes"
            )
            raise RateLimited(
                f"YouTube is rate-limiting this host "
                f"({exc.__class__.__name__} for video {video_id})",
                next=(
                    f"wait {when} and retry the same call; serialise "
                    "youtube fetches — one at a time, parallel bursts "
                    "are what trips the block"
                ),
            ) from exc
        except _LimiterRefused as exc:
            # The shared limiter refused a host:www.youtube.com token —
            # quota spent or the paced wait ran out (gr531240).
            raise RateLimited(
                f"youtube fetch paced out for {video_id}: {exc}",
                next=(
                    "wait ~60 s and retry the same call; serialise "
                    "youtube fetches — one at a time, parallel bursts "
                    "spend the shared rate bucket"
                ),
            ) from exc
        except Exception as exc:
            raise _upstream_error(
                exc,
                default_next=f"retry the same call; if it persists, inspect "
                f"get(kind='youtube', id='{video_id}', view='languages')",
                retry_call=f"get(kind='youtube', id='{video_id}')",
            ) from exc

        # Time-windowed chunks (gr540142): a single unaddressable blob
        # used to leave any transcript over the context cap unreadable.
        snippet_list = list(snippets)
        blocks = _window_snippets(snippet_list) or [ChunkInsert(ord=0, text="")]
        # Best-effort scrape of the watch page for channel / title /
        # description / duration. If the scrape fails for any reason
        # (network, format drift, rate limit, SSRF guard) we still
        # return the transcript — the meta dict just keeps the small
        # set we already had.
        scraped_meta = _scrape_watch_page_meta(video_id)
        meta: dict[str, Any] = {
            "video_id": video_id,
            "languages": languages,
            "snippet_count": len(snippet_list),
        }
        meta.update(scraped_meta)
        title = scraped_meta.get("title") or f"YouTube transcript: {video_id}"
        return FetchResult(
            title=title,
            body_blocks=blocks,
            cost_usd=None,  # free
            meta=meta,
        )

    # ── render: append per-video deep-link below attribution ──────────

    def _render(self, ref, cache, *, hit):
        self._maybe_rechunk(ref)
        resp = super()._render(ref, cache, hit=hit)
        meta = cache.meta or {}
        video_id = meta.get("video_id") or ref.slug
        # Build a small header block above the body when the watch-page
        # scrape populated channel / duration / description. Keeps the
        # transcript searchable as the primary content but surfaces the
        # context every operator wants ("which channel is this from?").
        header_lines: list[str] = []
        if meta.get("channel_name"):
            channel_url = meta.get("channel_url")
            if channel_url:
                header_lines.append(f"Channel: {meta['channel_name']} ({channel_url})")
            else:
                header_lines.append(f"Channel: {meta['channel_name']}")
        if meta.get("duration_s"):
            sec = int(meta["duration_s"])
            mins, s = divmod(sec, 60)
            header_lines.append(f"Duration: {mins}m{s:02d}s")
        elif meta.get("duration_iso"):
            header_lines.append(f"Duration: {meta['duration_iso']}")
        if meta.get("published_at"):
            header_lines.append(f"Published: {meta['published_at']}")
        if meta.get("description"):
            # Cap the description preview — full text lives in the meta.
            desc = meta["description"][:400].rstrip()
            if len(meta["description"]) > 400:
                desc += "…"
            header_lines.append(f"Description: {desc}")

        deep_link = f"  Watch: https://www.youtube.com/watch?v={video_id}"
        body = resp.body
        if header_lines:
            body = "\n".join(header_lines) + "\n\n" + body
        return Response(
            body=body + "\n" + deep_link,
            cost=resp.cost,
            # The base's first-range hint (gr533135) is the one recovery.
            result_budget_hint=resp.result_budget_hint,
            # Thunk: the chunk map is loaded by the fitter only when the
            # context cap actually binds.
            budget_outline=lambda: self._budget_outline(ref),
        )

    # ── chunk selectors, toc, lazy re-chunk (gr540142) ────────────────

    @staticmethod
    def _selector_slug(prefix: str) -> str:
        """``~`` selector prefix → the ref slug (the bare video id)."""
        try:
            return _extract_video_id(prefix)
        except BadInput:
            return prefix.strip()

    def _render_chunk_selector(
        self,
        slug: str,
        lo: int,
        hi: int,
        *,
        tags: list[str] | None = None,
        untags: list[str] | None = None,
        view: str | None = None,
    ) -> Response:
        """Re-window a legacy one-blob transcript BEFORE the selector
        resolves its chunk (gr540142), then defer to the base's range
        read (gr533135). The re-chunk commits before the base re-reads
        the entry by slug, so the second lookup sees the new chunks."""
        cached = self.store.get_cache_entry_by_slug(kind=self.spec.kind, slug=slug)
        if cached is not None:
            self._maybe_rechunk(cached[0])
        return super()._render_chunk_selector(
            slug, lo, hi, tags=tags, untags=untags, view=view
        )

    def _chunk_label(self, slug: str, block: Any) -> str | None:
        """``slug~n`` plus the chunk's video time window, printed before
        each chunk of a range read so agents can cite video times."""
        meta = block.meta or {}
        if meta.get("start_s") is None:
            return None
        label = f"`{slug}~{block.ord}` {_fmt_ts(meta['start_s'])}"
        if meta.get("end_s") is not None:
            label += f"–{_fmt_ts(meta['end_s'])}"
        return label

    def _maybe_rechunk(self, ref: Ref) -> None:
        """Re-window a legacy one-blob transcript on first read.

        Transcripts cached before chunk selectors exist sit as a single
        unaddressable chunk; split it once so ``~`` selectors, the toc
        and the context cap all work. ``insert_chunks(replace=True)``
        is the sanctioned DELETE+INSERT replace — chunk rows are never
        UPDATEd. Timestamps can't be recovered from the stored text, so
        re-windowed chunks carry no ``start_s``.

        The replace is destructive to per-chunk identity: the legacy
        blob's ``chunks`` row — and with it the chunk id any
        ``<code><id>`` handle was minted from, plus its chunk tags and
        summaries — goes away (ON DELETE CASCADE); ``<video>~0`` then
        names the first re-windowed chunk, not the blob. Embeddings are
        refilled by the worker, which picks the fresh chunks up like any
        new ingest.

        Race discipline: two concurrent readers can both see the legacy
        blob, so check-and-replace runs in ONE transaction that locks
        the ``refs`` row (FOR UPDATE, ~2s ``lock_timeout``) and re-verifies
        the still-legacy predicate before replacing. A loser of that
        wait — or a unique violation that slips through anyway — serves
        whatever chunks are stored; this lazy path never raises to the
        caller. The leading ``count_chunks`` probe keeps already-chunked
        transcripts off this code entirely (one indexed count, not a
        full body load, per render).
        """
        if self.store.chunks.count_chunks(ref.id) != 1:
            return
        blocks = self.store.chunks.list_chunks_for_ref(ref.id)
        if not blocks:
            return
        blob = blocks[0]
        if blob.chunk_kind != "paragraph":
            return
        if (blob.meta or {}).get("start_s") is not None:
            return
        if len(blob.text.encode("utf-8")) <= _LEGACY_BLOB_MIN:
            return
        try:
            with self.store.tx() as conn:
                conn.execute("SET LOCAL lock_timeout = '2s'")
                conn.execute(
                    "SELECT 1 FROM refs WHERE ref_id = %s FOR UPDATE", (ref.id,)
                )
                # Re-check under the lock: a sibling reader may have
                # committed its own re-window while we waited.
                still = conn.execute(
                    "SELECT count(*) FROM chunks "
                    "WHERE ref_id = %s AND ord >= 0 "
                    "AND chunk_kind = 'paragraph' "
                    "AND meta->>'start_s' IS NULL "
                    "AND NOT (meta ? 'legacy_rechunk')",
                    (ref.id,),
                ).fetchone()
                if still is None or int(still[0]) != 1:
                    return
                self.store.chunks.insert_chunks(
                    ref.id, _split_blob_window(blob.text), replace=True, conn=conn
                )
        except psycopg.Error:
            log.warning(
                "youtube: lazy re-chunk of ref %d abandoned "
                "(lock wait or concurrent replace); serving stored chunks",
                ref.id,
                exc_info=True,
            )

    def _render_toc(self, *, id: str | int | None, q: str | None) -> Response:
        """Chunk map of a cached transcript — selector, time window and
        size per row (gr540142). Read-only: never fetches."""
        video_input = self._coerce_query(id, q)
        slug = self._selector_slug(video_input)
        cached = self.store.get_cache_entry_by_slug(kind=self.spec.kind, slug=slug)
        if cached is None:
            raise NotFound(
                f"youtube {slug!r} is not cached yet — nothing to outline",
                next=(
                    f"get(kind='youtube', id='{slug}') to fetch the transcript first"
                ),
            )
        ref, _cache = cached
        self._maybe_rechunk(ref)
        blocks = self.store.chunks.list_chunks_for_ref(ref.id)
        if not blocks:
            raise NotFound(
                f"youtube {slug!r} has no stored transcript chunks",
                next=f"get(kind='youtube', id='{slug}', refresh=True)",
            )
        total = sum(len(b.text.encode("utf-8")) for b in blocks)
        first_meta = blocks[0].meta or {}
        last = blocks[-1]
        span = ""
        if first_meta.get("start_s") is not None:
            span = (
                f" · {_fmt_ts(first_meta['start_s'])}"
                f"–{_fmt_ts((last.meta or {}).get('end_s'))}"
            )
        lines = [
            f"# {ref.title} — transcript map",
            f"_{len(blocks)} chunks · {total} bytes{span}_",
            "",
        ]
        # Keep the map itself under the context cap: emit rows until a
        # byte budget, then a ``rest:`` line naming the exact range the
        # omitted rows point at (the map is an index of ``~`` reads).
        used = 0
        cut_at: int | None = None
        for b in blocks:
            row = self._toc_row(slug, b)
            cost = len(row.encode("utf-8")) + 3
            if used + cost > _TOC_ROW_BUDGET:
                cut_at = b.ord
                break
            lines.append(f"  {row}")
            used += cost
        if cut_at is not None:
            lines.append("")
            lines.append(f"rest: get(kind='youtube', id='{slug}~{cut_at}..{last.ord}')")
        body = "\n".join(lines) + render_next_section(
            [
                (
                    f"get(kind='youtube', id='{slug}~A..B')",
                    "read a chunk range",
                ),
                (
                    f"get(kind='youtube', id='{slug}', view='body')",
                    "read the whole transcript (cut to the cap)",
                ),
            ]
        )
        return Response(body=body)

    @staticmethod
    def _toc_row(slug: str, b: ChunkRow) -> str:
        """One toc/outline row: ``slug~ord``, time window, size."""
        from precis._result_budget import size_cell

        meta = b.meta or {}
        ts = ""
        if meta.get("start_s") is not None:
            end = meta.get("end_s")
            ts = f"  {_fmt_ts(meta['start_s'])}"
            if end is not None:
                ts += f"–{_fmt_ts(end)}"
        sz = size_cell(len(b.text.encode("utf-8")))
        return f"`{slug}~{b.ord}`{ts}" + (f"  {sz}" if sz else "")

    def _budget_outline(self, ref: Ref) -> str:
        """``slug~N`` selector lines for the budget fitter's outline
        block — the map a truncated whole-transcript serve prints."""
        slug = ref.slug or str(ref.id)
        blocks = self.store.chunks.list_chunks_for_ref(ref.id)
        if len(blocks) <= 1:
            return ""
        return "\n".join(self._toc_row(slug, b) for b in blocks)


# ---------------------------------------------------------------------------
# Listing / search display helpers
# ---------------------------------------------------------------------------


def _video_listing_bits(cache: CacheEntry) -> list[str]:
    """The ``channel · duration · cached <date> · CACHE:state`` tail of a
    video row — shared by the /recent listing and search hit cards."""
    meta = cache.meta or {}
    bits: list[str] = []
    if meta.get("channel_name"):
        bits.append(str(meta["channel_name"]))
    if meta.get("duration_s"):
        mins, s = divmod(int(meta["duration_s"]), 60)
        bits.append(f"{mins}m{s:02d}s")
    elif meta.get("duration_iso"):
        bits.append(str(meta["duration_iso"]))
    if cache.fetched_at is not None:
        bits.append(f"cached {cache.fetched_at.strftime('%Y-%m-%d')}")
    if cache.fresh_until is None:
        state = "CACHE:pinned"
    elif cache.fresh_until > datetime.now(UTC):
        state = "CACHE:fresh"
    else:
        state = "CACHE:stale"
    bits.append(state)
    return bits


# ---------------------------------------------------------------------------
# Side queries (not cached — they're cheap metadata)
# ---------------------------------------------------------------------------


def _list_languages(video_id: str) -> Response:
    """Return the available transcript languages for a video.

    Not stored in `cache_state` — it's a metadata query that's small
    and changes if the uploader adds tracks. Each call hits the API
    directly.
    """
    yt = require_optional("youtube_transcript_api")
    try:
        api = yt.YouTubeTranscriptApi()
        transcripts = call_with_backoff(
            lambda: _paced_call(lambda v: list(api.list(v)), video_id),
            service="youtube",
            policy=_yt_policy("youtube"),
            url=_YT_HOST_URL,
        )
    except Exception as exc:
        raise _upstream_error(
            exc,
            default_next="retry the same call shortly; YouTube may be throttling",
            retry_call=f"get(kind='youtube', id='{video_id}', view='languages')",
        ) from exc

    lines = [f"Available transcripts for {video_id}:"]
    for t in transcripts:
        mark = "auto" if t.is_generated else "human"
        lines.append(f"  {t.language_code:<6} {t.language:<30} [{mark}]")
    lines.append("")
    lines.append(f"- {_YT_BASE_ATTRIBUTION}")
    lines.append(f"  Watch: https://www.youtube.com/watch?v={video_id}")
    body = "\n".join(lines)
    # Interpolate the first listed track's real language code — the
    # table just above prints real codes, so a hardcoded ``LANG_CODE``
    # placeholder is a copy-paste trap (falls back to the placeholder
    # only in the no-tracks edge case, where nothing real exists to
    # substitute).
    first_code = transcripts[0].language_code if transcripts else "LANG_CODE"
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
                f"args={{'languages': {first_code!r}}})",
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


# ---------------------------------------------------------------------------
# Transcript windowing (gr540142)
# ---------------------------------------------------------------------------


def _fmt_ts(sec: float | None) -> str:
    """Seconds → ``m:ss`` (``h:mm:ss`` past the hour)."""
    s = int(sec or 0)
    h, r = divmod(s, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _window_snippets(snippets: list[Any]) -> list[ChunkInsert]:
    """Group caption snippets into ~``_TRANSCRIPT_CHUNK_TARGET``-byte
    chunks, each stamped with the time window it covers.

    Cuts happen only on snippet boundaries — a caption line is never
    split mid-sentence — and each chunk carries ``start_s``/``end_s``
    meta so the toc rows and the range-read label lines (``_chunk_label``)
    can name the video position the chunk covers.
    """
    out: list[ChunkInsert] = []
    cur: list[str] = []
    cur_bytes = 0
    win_start: float | None = None
    win_end: float | None = None

    def _flush() -> None:
        nonlocal cur, cur_bytes, win_start, win_end
        if not cur:
            return
        meta: dict[str, Any] = {}
        if win_start is not None:
            meta["start_s"] = round(win_start, 1)
        if win_end is not None:
            meta["end_s"] = round(win_end, 1)
        out.append(ChunkInsert(ord=len(out), text="\n".join(cur), meta=meta))
        cur = []
        cur_bytes = 0
        win_start = None
        win_end = None

    for s in snippets:
        t = (getattr(s, "text", None) or "").strip()
        if not t:
            continue
        st = getattr(s, "start", None)
        du = getattr(s, "duration", 0) or 0
        cost = len(t.encode("utf-8")) + 1
        if cur and cur_bytes + cost > _TRANSCRIPT_CHUNK_TARGET:
            _flush()
        if win_start is None and st is not None:
            win_start = st
        cur.append(t)
        cur_bytes += cost
        if st is not None:
            win_end = st + du
    _flush()
    return out


def _split_long_line(ln: str, limit: int) -> list[str]:
    """One input line → segments each ≤ ``limit`` bytes, cut on spaces.

    A legacy blob can hold a whole un-wrapped paragraph on one line
    (auto-captions arrive without newlines); leaving it whole would mint
    a chunk that itself blows the context cap. A single word longer than
    ``limit`` stays whole — there is no boundary to cut at.
    """
    if len(ln.encode("utf-8")) <= limit:
        return [ln]
    out: list[str] = []
    cur = ""
    for word in ln.split(" "):
        cand = word if not cur else f"{cur} {word}"
        if cur and len(cand.encode("utf-8")) > limit:
            out.append(cur)
            cur = word
        else:
            cur = cand
    if cur:
        out.append(cur)
    return out


def _split_blob_window(text: str) -> list[ChunkInsert]:
    """Time-less fallback for a legacy single-blob transcript: split on
    newline boundaries into ~``_TRANSCRIPT_CHUNK_TARGET``-byte chunks.
    ``legacy_rechunk`` marks the windowing pass that produced them."""
    out: list[ChunkInsert] = []
    cur: list[str] = []
    cur_bytes = 0
    for ln in text.split("\n"):
        for seg in _split_long_line(ln, _TRANSCRIPT_CHUNK_TARGET):
            cost = len(seg.encode("utf-8")) + 1
            if cur and cur_bytes + cost > _TRANSCRIPT_CHUNK_TARGET:
                out.append(
                    ChunkInsert(
                        ord=len(out),
                        text="\n".join(cur),
                        meta={"legacy_rechunk": True},
                    )
                )
                cur = []
                cur_bytes = 0
            cur.append(seg)
            cur_bytes += cost
    if cur:
        out.append(
            ChunkInsert(
                ord=len(out),
                text="\n".join(cur),
                meta={"legacy_rechunk": True},
            )
        )
    return out or [ChunkInsert(ord=0, text=text)]


# ---------------------------------------------------------------------------
# Watch-page meta scrape
# ---------------------------------------------------------------------------


#: Match ``og:`` / ``itemprop`` meta tags. The HTML format YouTube ships is
#: ``<meta property="og:title" content="…">`` / ``<meta itemprop="name"
#: content="…">`` etc. We accept either attribute ordering and either
#: quoting style.
_META_RE = re.compile(
    r'<meta\b[^>]*?(?:property|name|itemprop)="([^"]+)"[^>]*?content="([^"]*)"',
    re.IGNORECASE,
)

#: Channel name lives in a ``<link itemprop="name" content="…">`` tag inside
#: the ``<span itemprop="author">`` block. Same regex shape works because we
#: already capture ``itemprop`` → content above; we look for the channel
#: link separately via this pattern.
_AUTHOR_LINK_RE = re.compile(
    r'<link\b[^>]*?itemprop="url"[^>]*?href="([^"]+)"',
    re.IGNORECASE,
)


def _scrape_watch_page_meta(video_id: str) -> dict[str, Any]:
    """Pull title / channel / duration / description from the watch page.

    Returns an empty dict on any failure path — the caller treats
    missing meta as "no scrape available" and falls back to what we
    already have. Outbound HTTP goes through ``safe_get`` per the
    SSRF guard convention.
    """
    try:
        import httpx

        from precis.utils.http import polite_user_agent, with_backoff
        from precis.utils.safe_fetch import pinning_transport, safe_get
    except ImportError:
        return {}
    url = f"https://www.youtube.com/watch?v={video_id}"
    # Built inline (not via http_client) to keep the httpx-missing path a
    # silent ``return {}`` above — http_client raises Upstream, not
    # ImportError. Same SSRF guard: pinning_transport() carries the backend.
    try:
        with httpx.Client(
            follow_redirects=False,
            timeout=8.0,
            headers={"User-Agent": polite_user_agent()},
            transport=pinning_transport(),
        ) as client:
            # Politeness core, host-keyed (host:www.youtube.com, 1/s): limiter
            # token per attempt, backoff on 429/5xx + Retry-After. The scrape
            # is best-effort, so any failure (incl. exhausted retries, open
            # breaker) still lands in the ``return {}`` below.
            resp = with_backoff(
                lambda: safe_get(client, url),
                service="youtube-scrape",
                policy=_yt_policy("youtube-scrape", cap=5.0),
                url=url,
            )
    except Exception:
        log.warning("youtube: watch-page scrape failed for %s", video_id, exc_info=True)
        return {}
    if resp.status_code != 200:
        return {}
    html = resp.text
    meta: dict[str, Any] = {}
    for m in _META_RE.finditer(html):
        key = m.group(1).strip().lower()
        val = m.group(2)
        if not val:
            continue
        # og:title / og:description / og:image / og:url
        if key == "og:title" and "title" not in meta:
            meta["title"] = _decode_html_entities(val)
        elif key == "og:description" and "description" not in meta:
            meta["description"] = _decode_html_entities(val)
        elif key == "og:image" and "thumbnail_url" not in meta:
            meta["thumbnail_url"] = val
        elif key == "og:video:duration" and "duration_s" not in meta:
            try:
                meta["duration_s"] = int(val)
            except ValueError:
                pass
        # Schema.org itemprops: name (channel), datePublished
        elif key == "name" and "channel_name" not in meta:
            meta["channel_name"] = _decode_html_entities(val)
        elif key == "datepublished" and "published_at" not in meta:
            meta["published_at"] = val
        elif key == "duration" and "duration_iso" not in meta:
            meta["duration_iso"] = val
    # Channel URL — first author-itemprop link.
    author_m = _AUTHOR_LINK_RE.search(html)
    if author_m is not None and "channel_url" not in meta:
        meta["channel_url"] = author_m.group(1)
    meta["watch_url"] = url
    return meta


_ENTITY_MAP = {
    "&amp;": "&",
    "&lt;": "<",
    "&gt;": ">",
    "&quot;": '"',
    "&apos;": "'",
    "&#39;": "'",
}


def _decode_html_entities(s: str) -> str:
    """Decode the half-dozen entities that show up in YouTube meta tags.

    Defer to the stdlib for full coverage when callers need it; this
    inline map keeps the scrape light-dependency-free.
    """
    from html import unescape

    return unescape(s)
