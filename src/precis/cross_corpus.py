"""Cross-corpus semantic search — ``type='all'`` and ``type='a,b,c'``.

Agents routinely want "find anything relevant to X" across their whole
precis footprint — papers, memories, bookmarks, books, todos, etc.
Before this module, every kind had its own isolated search and agents
had to fan out N calls + merge client-side.

This module provides a single dispatch entry-point used by
``server.search()`` whenever the caller passes ``type='all'`` or a
comma-separated list like ``type='papers,memories,websites'``.  It
resolves the kind list to a set of ``corpus_id``s, calls
:meth:`acatome_store.store.Store.search_text` with the ``corpora=``
kwarg (a single query, one ranking pass), and renders a unified
result grouped by kind.

Design choices:

- **Only ref-backed kinds** participate — kinds without a corpus_id
  (``websearch``, ``research``, ``think``, ``calc``, ``math``,
  ``youtube``) are external services that don't live in the store
  and can't share a ranking space.  They're excluded from
  ``type='all'`` expansion and rejected with a clear error when
  listed explicitly.
- **Unified ranking**, not round-robin.  One pgvector call returns
  the top-k across all selected corpora, sorted by distance.  The
  agent sees a single ordered list even though we render it grouped
  by kind for scannability.
- **Scoping is incompatible with ``type='all'``**.  ``scope=`` means
  "restrict to this one ref" — combining it with cross-corpus makes
  no sense, so we reject the combination up front.
- **Empty-list corpora raise, never silently match nothing.**  A
  ``type=','`` typo would otherwise return "no hits" with no signal
  that the agent's intent was mis-parsed.
"""

from __future__ import annotations

import logging
from typing import Any

from precis.protocol import CallContext, ErrorCode, PrecisError
from precis.registry import (
    ALIASES,
    CORPUS_PLUGINS,
    KINDS,
    PLUGINS,
    _discover,
    _format_error,
    resolve_alias,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Kind → corpus_id resolution
# ---------------------------------------------------------------------------


def is_cross_corpus_request(type_arg: str) -> bool:
    """Return True if ``type_arg`` asks for cross-corpus search.

    Three recognised shapes:
    - ``'all'`` — every ref-backed kind
    - ``'a,b,c'`` — explicit list (at least one comma)
    - ``' a , b '`` — whitespace tolerated around commas
    """
    if not type_arg:
        return False
    stripped = type_arg.strip()
    if stripped == "all":
        return True
    return "," in stripped


def expand_type_to_corpora(type_arg: str) -> list[str]:
    """Resolve ``type='all'`` or ``'a,b,c'`` to a list of corpus_ids.

    Raises:
        PrecisError(KIND_UNKNOWN): a listed kind isn't registered, or
            doesn't have a corpus (e.g. ``websearch``, ``calc``).
    """
    _discover()

    if type_arg.strip() == "all":
        # Every registered plugin that has a corpus_id contributes,
        # **filtered by the active PRECIS_KINDS mask** so operators
        # who've restricted the agent's visible kinds don't get their
        # mask silently bypassed by type='all'.  When no mask is set,
        # visible_kinds('search') returns every registered search-kind
        # and we end up with the same set as iterating CORPUS_PLUGINS.
        from precis.registry import visible_kinds as _visible_kinds

        visible_search_kinds = _visible_kinds("search")
        corpora_set: set[str] = set()
        for rk in visible_search_kinds:
            plugin = PLUGINS.get(rk.plugin_name)
            if plugin and plugin.corpus_id:
                corpora_set.add(plugin.corpus_id)
        corpora = sorted(corpora_set)
        if not corpora:
            raise PrecisError(
                ErrorCode.KIND_UNAVAILABLE,
                cause=(
                    "type='all' selected no corpora — either no ref-backed "
                    "plugins are registered, or the PRECIS_KINDS mask hides "
                    "every corpus-backed kind from search"
                ),
                next=(
                    "install precis-mcp[paper] to enable the paper corpus, "
                    "or check PRECIS_KINDS (see stats() for current visibility)"
                ),
            )
        return corpora

    # Comma-separated list: split, strip, drop empties.
    raw_kinds = [part.strip() for part in type_arg.split(",")]
    raw_kinds = [k for k in raw_kinds if k]
    if not raw_kinds:
        raise PrecisError(
            ErrorCode.KIND_UNKNOWN,
            cause=f"type={type_arg!r} parsed to an empty kind list",
            next="pass type='all' for every corpus, or list kinds like type='paper,memory'",
        )

    corpora: list[str] = []
    for kind in raw_kinds:
        corpus_id = kind_to_corpus_id(kind)
        if corpus_id is None:
            # Kind exists but isn't backed by a corpus.
            if kind in KINDS or kind in ALIASES:
                raise PrecisError(
                    ErrorCode.KIND_UNKNOWN,
                    cause=(
                        f"kind {kind!r} is not ref-backed and can't participate "
                        "in cross-corpus search (external services like "
                        "websearch / research / calc have no corpus)"
                    ),
                    next=(
                        "drop it from the type= list, or call it directly as "
                        f"search(query='…', type='{kind}')"
                    ),
                )
            raise PrecisError(
                ErrorCode.KIND_UNKNOWN,
                cause=f"unknown kind: {kind!r}",
                next="see stats() for the list of registered kinds",
            )
        if corpus_id not in corpora:  # dedupe; preserve order
            corpora.append(corpus_id)

    return corpora


def kind_to_corpus_id(kind: str) -> str | None:
    """Return the corpus_id a kind is bound to, or ``None``.

    Accepts both the canonical singular kind name (``paper``,
    ``memory``, ``web``, ``book``) and the plural corpus id
    (``papers``, ``memories``, ``websites``, ``books``).  Users
    reach for both forms interchangeably — rejecting the plural
    form was a friction point surfaced immediately after
    ``type='paper,book'`` shipped.

    Kinds without a corpus (``websearch``, ``research``, ``think``,
    ``calc``, ``math``, ``youtube``, ``skill``) return None — they're
    external services or pure-compute plugins.
    """
    _discover()
    # Direct corpus_id hit: ``papers``, ``memories``, ``websites``, …
    if kind in CORPUS_PLUGINS:
        return kind
    # Otherwise treat as a kind name and look up the plugin's corpus.
    canonical = resolve_alias(kind) or kind
    spec = KINDS.get(canonical)
    if spec is None:
        return None
    plugin = PLUGINS.get(spec.plugin_name)
    if plugin is None:
        return None
    return plugin.corpus_id or None


def corpus_id_to_kind(corpus_id: str) -> str | None:
    """Inverse of :func:`kind_to_corpus_id` — for rendering hit labels.

    Returns the *first* kind the corpus's plugin exposes.  A plugin
    may register multiple kinds (a future feature), but for labelling
    purposes the primary kind is always the one matching the plugin
    name or the lexicographically-first kind.
    """
    plugin = CORPUS_PLUGINS.get(corpus_id)
    if plugin is None:
        return None
    # Plugin exposes kinds via its registered kind_specs; grab the
    # first one back-referenced from KINDS that points at this plugin.
    candidates = sorted(
        k for k, spec in KINDS.items() if spec.plugin_name == plugin.name
    )
    return candidates[0] if candidates else None


# ---------------------------------------------------------------------------
# Unified search dispatcher
# ---------------------------------------------------------------------------


# Per-kind emoji badges for rendering — keeps grouped output scannable
# at a glance.  Only ref-backed kinds are listed; fall back to 📁 for
# anything we don't have a specific icon for.
_KIND_BADGES: dict[str, str] = {
    "paper": "📄",
    "papers": "📄",
    "memory": "💭",
    "memories": "💭",
    "todo": "✅",
    "todos": "✅",
    "flashcard": "🎴",
    "flashcards": "🎴",
    "conversation": "💬",
    "conversations": "💬",
    "web": "🌐",
    "websites": "🌐",
    "book": "📚",
    "books": "📚",
    "skill": "🔧",
    "skills": "🔧",
    "quest": "🎯",
    "quests": "🎯",
    "note": "📝",
    "notes": "📝",
    "wiki": "🔖",
}


def _badge_for(kind_or_corpus: str) -> str:
    return _KIND_BADGES.get(kind_or_corpus, "📁")


def search_across_corpora(
    *,
    query: str,
    corpora: list[str],
    top_k: int,
    scope: str = "",
) -> str:
    """Run a single cross-corpus semantic search.

    Args:
        query: Natural-language query string.
        corpora: List of corpus_ids to search (non-empty).
        top_k: Max hits across *all* corpora combined.
        scope: Must be empty — cross-corpus is incompatible with
            ref-level scoping.  A non-empty scope raises.

    Returns:
        Rendered string grouped by kind, ordered by distance within
        each group.
    """
    if scope:
        raise PrecisError(
            ErrorCode.KIND_UNKNOWN,
            cause=(
                "scope= is incompatible with cross-corpus search — "
                "scope restricts to one ref, which contradicts 'search everywhere'"
            ),
            next=(
                "drop scope= for cross-corpus, or use a single "
                f"type= (e.g. type='paper', scope={scope!r})"
            ),
        )

    from precis._store import get_store

    store = get_store()
    try:
        hits = store.search_text(query, top_k=top_k, corpora=corpora)
    except (ImportError, ModuleNotFoundError) as exc:
        raise PrecisError(
            ErrorCode.KIND_UNAVAILABLE,
            cause=f"semantic search unavailable: {exc}",
            next="install sentence-transformers or configure a vector backend",
        ) from exc

    return _render_cross_corpus_hits(query, corpora, hits)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _render_cross_corpus_hits(
    query: str,
    corpora: list[str],
    hits: list[dict[str, Any]],
) -> str:
    """Format hits grouped by kind for the agent.

    Each hit carries ``metadata.corpus_id`` (from the store's
    Block→Ref JOIN on pgvector, or the metadata stamp on Chroma).
    We group by that, sort within each group by ``distance`` asc,
    and prefix each group with a kind-specific emoji badge.
    """
    header = f"🔍 Cross-corpus: '{query}' across {len(corpora)} corpora"

    if not hits:
        return (
            f"{header}\n\n"
            f"No hits in: {', '.join(corpora)}\n"
            "Try: broader query, more corpora via type='all', "
            "or check that content has been embedded (acatome-store backfill-embeddings)"
        )

    # Bucket by corpus_id.  Unknown/missing corpus_id bucket under
    # '_other' so we still surface the hit instead of dropping it.
    buckets: dict[str, list[dict[str, Any]]] = {}
    for h in hits:
        cid = h.get("metadata", {}).get("corpus_id") or "_other"
        buckets.setdefault(cid, []).append(h)

    # Sort within each bucket by distance ascending.
    for bucket in buckets.values():
        bucket.sort(key=lambda h: h.get("distance", 1.0))

    # Render corpora in the order the caller requested (predictable),
    # then any unexpected buckets at the end.
    ordered: list[str] = [c for c in corpora if c in buckets]
    ordered += [c for c in buckets if c not in ordered]

    lines: list[str] = [f"{header} — {len(hits)} hits", ""]
    for corpus_id in ordered:
        bucket = buckets[corpus_id]
        kind = corpus_id_to_kind(corpus_id) or corpus_id
        badge = _badge_for(corpus_id)
        lines.append(f"{badge} {kind.capitalize()} ({len(bucket)})")
        for h in bucket:
            meta = h.get("metadata", {})
            slug = meta.get("slug") or meta.get("node_id") or "?"
            dist = h.get("distance", 0.0)
            # Include a short text snippet — handy for the agent's
            # ranking decisions without needing a follow-up get().
            text = (h.get("text") or "").strip().replace("\n", " ")
            snippet = (text[:140] + "…") if len(text) > 140 else text
            title_bit = meta.get("ref_title") or ""
            head = f"  • {slug}"
            if title_bit and title_bit != slug:
                head += f" — {title_bit}"
            lines.append(f"{head}  [d={dist:.2f}]")
            if snippet:
                lines.append(f"    {snippet}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Error rendering helper — mirrors server._format_error's shape so the
# cross-corpus path produces identical error envelopes.
# ---------------------------------------------------------------------------


def format_cross_corpus_error(
    exc: PrecisError,
    *,
    query: str,
    type_arg: str,
    top_k: int,
) -> str:
    """Format a :class:`PrecisError` raised by the cross-corpus path
    into the standard ERROR envelope the rest of the server emits."""
    ctx = CallContext(
        kind="all",
        verb="search",
        args={"type": type_arg, "query": query, "top_k": top_k},
    )
    return _format_error(
        exc.code,
        ctx,
        cause=exc.cause or str(exc),
        options=list(exc.options) if exc.options else None,
        next_hint=exc.next,
    )
