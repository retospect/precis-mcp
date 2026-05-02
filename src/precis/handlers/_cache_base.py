"""Base class for cache-backed kinds (`math`, `youtube`, `web`, …).

Cache-backed kinds wrap an external paid (or rate-limited) tool. They
share a single architectural pattern:

1. Compute a stable `request_hash` from the user's query.
2. Look up `(provider, request_hash)` in `cache_state`.
3. On hit within TTL → return the cached body.
4. On miss / stale → call subclass-defined `_fetch(key)`, store the
   result via `Store.put_cache_entry`, return the fresh body.
5. Always render a legal-attribution footer.

Subclasses provide:

- ``provider``: matches one row in the `providers` table.
- ``ttl_seconds``: default cache lifetime in seconds (or `None` to pin).
- ``attribution``: per-provider legal text rendered as the response
  footer on every call (cached or not).
- ``corpus_slug``: which corpus stores the cached refs.
- ``_canonical_key(query)``: turn a user query into the deterministic
  cache key (used for `request_hash` and for the ref slug).
- ``_fetch(key)``: do the actual remote call. Returns a
  `FetchResult(title, body_blocks, model, cost_usd, meta)`.

The base does not concern itself with HTTP, JSON parsing, or
attribution wording — those are handler-specific. It owns only the
cache-flow plumbing.
"""

from __future__ import annotations

import hashlib
from abc import abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar

from precis.dispatch import Hub, InitError
from precis.errors import BadInput, NotFound
from precis.handlers._link_tag_ops import (
    apply_link_ops,
    apply_tag_ops,
    format_link_tag_ack,
)
from precis.protocol import Handler
from precis.response import Response
from precis.store import SEMANTIC_DISTANCE_FLOOR
from precis.store.types import BlockInsert
from precis.utils.block_ingest import to_block_inserts
from precis.utils.md_parse import block_meta, parse_markdown
from precis.utils.next_block import render_next_section
from precis.utils.search_header import format_search_headline
from precis.utils.search_merge import SearchHit, block_hits_to_search_hits
from precis.utils.text import excerpt as _excerpt

if TYPE_CHECKING:
    from precis.store.types import CacheEntry, Ref


# ---------------------------------------------------------------------------
# Subclass return type for `_fetch`
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FetchResult:
    """What a cache-backed handler's `_fetch` returns.

    The base class lifts these into a `cache_state` row + a freshly
    inserted ref + body blocks.
    """

    title: str
    """Short human label for the ref. Renders as the response heading."""

    body_blocks: list[BlockInsert]
    """The cached body, sliced into blocks. One block is fine for short
    answers; transcripts / pages get many."""

    model: str | None = None
    """Model identifier when the provider exposes one (e.g. 'sonar',
    'wolfram-alpha')."""

    cost_usd: float | None = None
    """Per-call cost estimate. None for free providers."""

    meta: dict[str, Any] = field(default_factory=dict)
    """Extra structured metadata; lands in `cache_state.meta`."""


# ---------------------------------------------------------------------------
# Base handler
# ---------------------------------------------------------------------------


class CacheBackedHandler(Handler):
    """Shared cache flow for paid-tool / rate-limited kinds.

    Subclass contract:

        provider:       str           — one of the rows in `providers`
        ttl_seconds:    int | None    — default freshness; None = pin
        attribution:    str           — legal footer text
        corpus_slug:    str           — corpus to store cached refs in

        def _canonical_key(query: str) -> str: ...
        def _fetch(key: str) -> FetchResult: ...

    Everything else is provided.
    """

    provider: ClassVar[str]
    ttl_seconds: ClassVar[int | None]
    attribution: ClassVar[str]
    corpus_slug: ClassVar[str] = "default"

    #: One-line example query for this kind, used by error hints and
    #: empty-listing trailers. Each subclass overrides with something
    #: idiomatic (a Wolfram fact, a YouTube id, a URL …) so a 7B caller
    #: hitting the same hardcoded ``population of Ireland`` regardless
    #: of which cache kind they called doesn't run a Wolfram query when
    #: they meant to fetch a URL. (MCP critic MAJOR — hint hardcoded.)
    example_query: ClassVar[str] = "your query"

    def __init__(self, *, hub: Hub) -> None:
        if hub.store is None:
            raise InitError(f"{self.provider}: store required")
        self.store = hub.store
        # Embedder is optional. Cache-backed kinds that declare
        # ``supports_search`` need it to vectorize the query leg,
        # and ``_blocks_from_report`` needs it to embed extracted
        # body blocks. Subclasses that don't touch either (e.g.
        # math today) can ignore the attribute entirely. None is
        # the valid stateless / test-before-embedder-wired shape.
        self.embedder = hub.embedder

    # ── public verb (default `get` implementation) ─────────────────────

    def get(  # type: ignore[override]
        self,
        *,
        id: str | int | None = None,
        q: str | None = None,
        view: str | None = None,
        **_kw: Any,
    ) -> Response:
        # Bare get / "/" / "/recent" → listing of the most recent refs.
        # Promoted from PerplexityHandler so every cache-backed kind
        # behaves the same (math/web/youtube used to BadInput here).
        # (MCP critic MAJOR — inconsistent bare-get behaviour.)
        if self._is_listing_request(id, q):
            return self._render_recent()
        if isinstance(id, str) and id.startswith("/"):
            raise BadInput(
                f"unknown view {id!r} for kind={self.spec.kind!r}",
                options=["/", "/recent"],
                next=(
                    f"get(kind={self.spec.kind!r}, id='/recent') to list recent refs"
                ),
            )

        query = self._coerce_query(id, q)
        key = self._canonical_key(query)
        request_hash = self._hash(key)

        cached = self.store.get_cache_entry(
            provider=self.provider, request_hash=request_hash
        )
        if cached is not None and self._is_fresh(cached[1]):
            ref, cache = cached
            return self._render(ref, cache, hit=True)

        # Miss or stale — call upstream.
        result = self._fetch(key)
        ref, cache = self.store.put_cache_entry(
            corpus_id=self.store.ensure_corpus(self.corpus_slug),
            kind=self.spec.kind,
            slug=self._slug_for(key),
            title=result.title,
            body_blocks=result.body_blocks,
            provider=self.provider,
            request_hash=request_hash,
            ttl_seconds=self.ttl_seconds,
            model=result.model,
            cost_usd=result.cost_usd,
            cache_meta=result.meta,
        )
        return self._render(ref, cache, hit=False)

    @staticmethod
    def _is_listing_request(id: str | int | None, q: str | None) -> bool:
        """Decide whether bare get should produce a /recent listing.

        Treat any of these as the listing shape:

        * ``id`` is missing **and** ``q`` is missing/blank,
        * ``id`` is an empty/whitespace string (``id=''``, ``id='   '``),
        * ``id`` is the explicit list-view path ``'/'`` or ``'/recent'``.

        The empty-string branch matches the previous Perplexity behaviour
        — a 7B caller that learned ``get(kind='memory')`` from one kind
        often retries it as ``get(kind=X, id='')`` when the schema demands
        an id. Bouncing them with BadInput vs. serving the listing is
        the difference between a footgun and a useful default.
        """
        if id is None and not (isinstance(q, str) and q.strip()):
            return True
        if isinstance(id, str) and id.strip() in ("", "/", "/recent"):
            return True
        return False

    # ── subclass hooks ────────────────────────────────────────────────

    @abstractmethod
    def _canonical_key(self, query: str) -> str:
        """Normalize the user's query into a stable cache key.

        Examples:
            math:     'population of Ireland' → 'population of ireland'
            youtube:  'https://youtu.be/X' → 'X'
            web:      URL → canonicalize_url(URL)
        """

    @abstractmethod
    def _fetch(self, key: str) -> FetchResult:
        """Call the upstream provider. Synchronous. Raises on failure."""

    # ── default helpers, overridable ──────────────────────────────────

    def _is_fresh(self, cache: CacheEntry) -> bool:
        """Is this cache entry within its TTL?"""
        if cache.fresh_until is None:
            return True  # pinned
        return cache.fresh_until > datetime.now(UTC)

    def _slug_for(self, key: str) -> str:
        """Default ref slug = first 64 chars of the canonical key with a
        short hash suffix to keep slugs unique. Subclasses can override
        for prettier slugs (e.g. youtube uses the bare video id)."""
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:8]
        prefix = "".join(c if c.isalnum() or c in "-_" else "-" for c in key)[:64]
        return f"{prefix}-{digest}".strip("-")

    @staticmethod
    def _hash(key: str) -> str:
        """SHA-256 hex digest used as `cache_state.request_hash`."""
        return hashlib.sha256(key.encode("utf-8")).hexdigest()

    def _coerce_query(self, id: str | int | None, q: str | None) -> str:
        """Pull a query string from `id=` or `q=`. One must be set.

        The recovery hint names the *caller's* kind and the kind's own
        example query — not a hardcoded ``kind='math', q='population of
        Ireland'`` (a footgun that nudged 7B callers from web/youtube
        into running paid Wolfram queries). (MCP critic MAJOR — hint
        D2/D6 inconsistency.)
        """
        if isinstance(id, str) and id.strip():
            return id.strip()
        if isinstance(q, str) and q.strip():
            return q.strip()
        raise BadInput(
            f"{self.spec.kind} requires a query as `id` or `q`",
            next=(f"get(kind={self.spec.kind!r}, q={self.example_query!r})"),
        )

    # ── response rendering ────────────────────────────────────────────

    def _render(self, ref: Ref, cache: CacheEntry, *, hit: bool) -> Response:
        """Render the cached body + attribution footer + cost trailer."""
        blocks = self.store.list_blocks_for_ref(ref.id)
        body_text = "\n\n".join(b.text for b in blocks).rstrip()

        lines: list[str] = []
        lines.append(f"# {ref.title}")
        lines.append("")
        lines.append(body_text)
        lines.append("")
        lines.append(f"— {self.attribution}")

        cost = self._cost_str(cache, hit=hit)
        return Response(body="\n".join(lines), cost=cost)

    def _cost_str(self, cache: CacheEntry, *, hit: bool) -> str:
        """Format the cost trailer.

        - free provider, hit/miss: '[cost: free]'
        - paid provider, miss:     '[cost: ~$X.XXX]'
        - paid provider, hit:      '[cost: ~$X.XXX — cached]'
        """
        if cache.cost_usd is None or cache.cost_usd == 0:
            return "[cost: free]"
        suffix = " — cached" if hit else ""
        return f"[cost: ~${cache.cost_usd:.4f}{suffix}]"

    # ── /recent listing — shared across cache-backed kinds ─────────────

    def _render_recent(self, *, limit: int = 20) -> Response:
        """List the most recent refs of this kind, newest first.

        Default implementation used by every cache-backed kind. Empty-
        state names the kind-specific example query so the next hint is
        actionable (rather than the previous "kind='math'" hardcoded
        suggestion). PerplexityHandler still overrides to surface
        tier-specific guidance, but math/web/youtube now agree on the
        listing shape. (MCP critic MAJOR — bare-get inconsistency.)
        """
        refs = self.store.list_refs(
            kind=self.spec.kind,
            provider=self.provider,
            limit=limit,
        )
        heading = f"# recent {self.spec.kind} refs"
        if not refs:
            body = f"{heading}\n\n_(no {self.spec.kind} refs yet.)_\n"
            body += render_next_section(
                [
                    (
                        f"get(kind={self.spec.kind!r}, q={self.example_query!r})",
                        "run a fresh query",
                    ),
                ]
            )
            return Response(body=body)

        lines: list[str] = [heading, ""]
        for ref in refs:
            day = ref.updated_at.strftime("%Y-%m-%d") if ref.updated_at else "—"
            title = ref.title
            if len(title) > 80:
                title = title[:77] + "..."
            lines.append(f"- `{ref.slug}` — {title}  _({day})_")
        lines.append("")
        lines.append(
            f"_showing {len(refs)} of at most {limit}. "
            f"Next: get(kind={self.spec.kind!r}, id='<slug>') to read one._"
        )
        return Response(body="\n".join(lines))

    # ── seven-verb surface: search / tag / link ───────────────────────
    #
    # Promoted from ``_PerplexityBase`` in the web-bookmark patch:
    # every cache-backed kind that stores embedded body blocks
    # benefits from lexical/semantic search, tag-based bookmarking,
    # and cross-linking to memory or papers. Subclasses opt in by
    # flipping ``supports_search`` / ``supports_tag`` /
    # ``supports_link`` in their ``KindSpec`` — the methods here
    # are inert until the dispatch table wires them.
    #
    # Kinds that don't store per-block content (``math`` returns a
    # single-line answer) can leave the flags off and these remain
    # available as an opt-in when they grow richer payloads.

    def _resolve_cache_slug(self, id: str | int) -> tuple[str, int]:
        """Coerce an agent-facing id to a ``(slug, ref_id)`` pair.

        Cache-backed kinds are slug-addressed; the agent sees the
        slug in the ``/recent`` listing (and in ``search`` hits)
        and passes it back as ``id=`` to ``tag`` / ``link``. A
        missing cache row is ``NotFound`` with an "fetch it first"
        hint — the slug doesn't exist until ``get(...)`` has
        populated the cache.
        """
        slug = str(id).strip()
        if not slug:
            raise BadInput(
                f"{self.spec.kind} ops require id= (the slug)",
                next=(
                    f"tag(kind={self.spec.kind!r}, id='<slug>', "
                    "add=['CACHE:pinned'])"
                ),
            )
        ref = self.store.get_ref(kind=self.spec.kind, id=slug)
        if ref is None:
            raise NotFound(
                f"{self.spec.kind} slug {slug!r} not found",
                next=(
                    f"get(kind={self.spec.kind!r}, id='<query>') first to "
                    "populate the cache, then tag/link the resulting slug"
                ),
            )
        return slug, ref.id

    def tag(  # type: ignore[override]
        self,
        *,
        id: str | int,
        add: list[str] | None = None,
        remove: list[str] | None = None,
        **_kw: Any,
    ) -> Response:
        """Add/remove tags on an existing cache slug.

        Primary use-case: bookmarking. ``tag(kind='web',
        id='<slug>', add=['bookmark'])`` flags a fetched page for
        later rediscovery via ``search`` with an open-tag filter
        or a direct ``/recent`` scan.
        """
        if not add and not remove:
            raise BadInput(
                f"tag(kind={self.spec.kind!r}, id=...) requires add= or remove=",
                next=(
                    f"tag(kind={self.spec.kind!r}, id='<slug>', "
                    "add=['CACHE:pinned'])"
                ),
            )
        slug, ref_id = self._resolve_cache_slug(id)
        n_added, n_removed = apply_tag_ops(
            self.store, self.spec.kind, ref_id, tags=add, untags=remove
        )
        return Response(
            body=format_link_tag_ack(
                kind=self.spec.kind,
                ref_label=slug,
                n_links_added=0,
                n_links_removed=0,
                n_tags_added=n_added,
                n_tags_removed=n_removed,
            )
        )

    def link(  # type: ignore[override]
        self,
        *,
        id: str | int,
        target: str | None = None,
        mode: str = "add",
        rel: str | None = None,
        **_kw: Any,
    ) -> Response:
        """Add or remove a link from this cache slug to another ref.

        Canonical form for ``target``: ``kind:id[~selector]`` —
        e.g. ``memory:158`` for "this web page is about the thing
        I remembered there", or ``paper:wang2020state`` for
        "supplementary reading for this paper". See
        ``precis-relations`` for the relation vocabulary.
        """
        if target is None:
            raise BadInput(
                f"link(kind={self.spec.kind!r}, id=...) requires target=",
                next=(
                    f"link(kind={self.spec.kind!r}, id='<slug>', "
                    "target='memory:123')"
                ),
            )
        if mode not in ("add", "remove"):
            raise BadInput(
                f"link mode must be 'add' or 'remove', got {mode!r}",
                options=["add", "remove"],
            )
        slug, ref_id = self._resolve_cache_slug(id)
        n_added, n_removed = apply_link_ops(
            self.store,
            ref_id,
            link=target if mode == "add" else None,
            unlink=target if mode == "remove" else None,
            rel=rel,
        )
        return Response(
            body=format_link_tag_ack(
                kind=self.spec.kind,
                ref_label=slug,
                n_links_added=n_added,
                n_links_removed=n_removed,
                n_tags_added=0,
                n_tags_removed=0,
            )
        )

    # ── search ─────────────────────────────────────────────────────

    def search(  # type: ignore[override]
        self,
        *,
        q: str | None = None,
        top_k: int = 10,
        **_kw: Any,
    ) -> Response:
        """Block-level fused search across cached entries of this kind.

        Hybrid lexical + semantic (when an embedder is wired) using
        :meth:`Store.search_blocks_fused`. Subclasses whose
        ``_fetch`` stores only a single un-embedded block (no call
        to :meth:`_blocks_from_report`) will land lexical hits
        only — still useful for URL / title grep.
        """
        if q is None or not q.strip():
            raise BadInput(
                f"search requires q= for kind={self.spec.kind!r}",
                next=f"search(kind={self.spec.kind!r}, q='your query')",
            )

        query_vec: list[float] | None = None
        if self.embedder is not None:
            query_vec = self.embedder.embed_one(q)

        hits = self.store.search_blocks_fused(
            q=q,
            query_vec=query_vec,
            kind=self.spec.kind,
            limit=top_k,
            max_distance=SEMANTIC_DISTANCE_FLOOR,
        )
        if not hits:
            return Response(
                body=(
                    f"no {self.spec.kind} blocks match {q!r}\n"
                    f"next: get(kind={self.spec.kind!r}, "
                    f"id={self.example_query!r}) to populate the cache, "
                    f"or try a broader query"
                )
            )

        total = self.store.count_blocks_lexical(q=q, kind=self.spec.kind)
        lines = [
            format_search_headline(
                n_returned=len(hits),
                total=total,
                noun="block hit",
                query=q,
            )
        ]
        for block, ref, score in hits:
            slug = ref.slug or "???"
            handle = f"{slug}~{block.slug or block.pos}"
            preview = _excerpt(block.text)
            lines.append(f"\n## {handle}  (score={score:.4f})")
            lines.append(f"_{ref.title}_")
            lines.append(preview)
        return Response(body="\n".join(lines))

    def search_hits(  # type: ignore[override]
        self,
        *,
        q: str,
        top_k: int = 10,
        **_kw: Any,
    ) -> list[SearchHit]:
        """Block-level fused search as :class:`SearchHit` rows.

        Used by the dispatcher for cross-kind fans
        (``kind='*'`` / ``kind='paper,web'``). Empty queries return
        ``[]`` rather than raising — cross-kind callers tolerate a
        kind contributing zero rows.
        """
        if not (q and q.strip()):
            return []
        query_vec: list[float] | None = None
        if self.embedder is not None:
            query_vec = self.embedder.embed_one(q)
        triples = self.store.search_blocks_fused(
            q=q,
            query_vec=query_vec,
            kind=self.spec.kind,
            limit=top_k,
            max_distance=SEMANTIC_DISTANCE_FLOOR,
        )
        return block_hits_to_search_hits(triples, kind=self.spec.kind)

    # ── block ingestion helper ────────────────────────────────────

    def _blocks_from_report(self, body: str) -> list[BlockInsert]:
        """Parse a markdown body into embedded ``BlockInsert`` rows.

        Used by subclasses whose ``_fetch`` returns markdown-shaped
        content (Perplexity reports, trafilatura-extracted web
        pages). Each heading / paragraph / list / table / code
        fence becomes one block with a stable content-derived
        slug. Embedder runs per-block so search hits land on the
        matching chunk rather than the whole page.

        The empty-input branch returns a single un-embedded block
        wrapping the raw body so the cache row is never empty when
        the upstream returned something we couldn't parse.
        """
        md_blocks = parse_markdown(body)
        if not md_blocks:
            # Defensive fallback: parser found no structure → store
            # the whole text as one paragraph block (no embedding).
            # Better than dropping content entirely.
            return [BlockInsert(pos=0, text=body)]

        return to_block_inserts(
            md_blocks, embedder=self.embedder, meta_for=block_meta
        )


def _format_cache_footer(cache: CacheEntry) -> str:
    """Render the canonical cache annotation: ``age Nd · CACHE:state``.

    Mirrors the footer documented in the ``precis-cache`` skill.
    Used by handlers that want to surface cache status alongside
    their per-kind footer (e.g. web's ``Source: ...``).

    State derivation matches the ``cache_freshness`` view in
    ``0001_initial.sql``:

    - ``fresh_until is None`` → ``CACHE:pinned`` (never expires)
    - ``fresh_until > now``   → ``CACHE:fresh`` (within TTL)
    - else                    → ``CACHE:stale`` (past TTL — the
      handler will re-fetch on next miss)

    The age is the number of full days since ``fetched_at``, capped
    at 0 (so ``-0d`` from clock skew renders as ``0d``).
    """
    now = datetime.now(UTC)
    if cache.fetched_at is not None:
        age_days = max(0, (now - cache.fetched_at).days)
        age_str = f"age {age_days}d"
    else:
        age_str = "age ?"

    if cache.fresh_until is None:
        state = "CACHE:pinned"
    elif cache.fresh_until > now:
        state = "CACHE:fresh"
    else:
        state = "CACHE:stale"

    return f"{age_str} · {state}"


__all__ = [
    "CacheBackedHandler",
    "FetchResult",
    "_format_cache_footer",
]
