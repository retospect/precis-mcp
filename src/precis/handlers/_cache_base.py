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

from precis.errors import BadInput
from precis.protocol import Handler
from precis.response import Response
from precis.store.types import BlockInsert

if TYPE_CHECKING:
    from precis.store import Store
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

    def __init__(self, *, store: Store) -> None:
        self.store = store

    # ── public verb (default `get` implementation) ─────────────────────

    def get(  # type: ignore[override]
        self,
        *,
        id: str | int | None = None,
        q: str | None = None,
        view: str | None = None,
        **_kw: Any,
    ) -> Response:
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

    @staticmethod
    def _coerce_query(id: str | int | None, q: str | None) -> str:
        """Pull a query string from `id=` or `q=`. One must be set."""
        if isinstance(id, str) and id.strip():
            return id.strip()
        if isinstance(q, str) and q.strip():
            return q.strip()
        raise BadInput(
            "cache-backed kinds require a query as `id` or `q`",
            next="get(kind='math', q='population of Ireland')",
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
