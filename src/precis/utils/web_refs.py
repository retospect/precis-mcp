"""Stub ``web`` refs for cited URLs — mint-on-read, fetch-on-access (gr548801).

A perplexity body's trailing ``Sources:`` block lists ``[N] url`` rows —
the URLs the inline ``[N]`` markers index. The raw URL text gives an
agent nothing to follow, so each cited URL gets its own ``web`` cache
ref:

- The ref is minted as a **born-stale stub** (``ttl_seconds=0``) whose
  one-line body names the citing ref — no network at mint time.
- A later ``get(kind='web', id=URL)`` sees the stale row and re-fetches
  in place through ``polite_safe_get`` (the SSRF guard), preserving the
  ref id, slug, and any tags/links — that is the fetch-on-access.
- The rendered perplexity body rewrites each ``[N]`` marker to
  ``[web:<ref_id>]`` — the ``kind:id`` form ``linkify_refs`` turns into
  a ``/r/web/<id>`` anchor and ``mentions.resolve_handle_ref`` resolves
  on write. Stored body chunks stay untouched (append-only); the
  mapping is a render-time substitution.

``citation_pairs`` prefers the ``citation_map`` cache meta stamped at
fetch time; rows cached before it existed (and ``put(mode='import')``
rows, which carry no citation meta) fall back to parsing the
``Sources:`` block out of the body itself.
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from precis.errors import BadInput
from precis.store.types import ChunkInsert
from precis.utils.url import canonical_url, host_of, is_http_url, slug_from_url

if TYPE_CHECKING:
    from precis.store.store import Store

log = logging.getLogger(__name__)


#: The ``Sources:`` lead-in the fetch formatter appends as the last body
#: element (``handlers/perplexity.py::_format_perplexity_body``).
_SOURCES_LEADIN_RE = re.compile(r"^\s*Sources:\s*$")

#: One numbered citation row: ``[53] https://example.com/page``.
_SOURCE_ROW_RE = re.compile(r"^\s*\[(\d+)\]\s+(\S+)\s*$")

#: An inline citation marker ``[53]``. Guards: not a ``[[…]]`` authoring
#: bracket (``(?<!\[)``), not a markdown link ``[53](url)``, footnote
#: definition ``[53]:``, or adjacent bracket/digit.
_MARKER_RE = re.compile(r"(?<!\[)\[(\d+)\](?![\d(\]:])")


def parse_numbered_sources(text: str) -> list[tuple[int, str]]:
    """Parse ``[N] url`` rows under a ``Sources:`` lead-in.

    Scans the whole text — the render re-joins chunks with blank lines,
    so rows may be separated by empty lines; a run ends at the first
    non-blank line that isn't a row. Every ``Sources:`` block
    contributes; a repeated ``N`` keeps its first URL.
    """
    pairs: list[tuple[int, str]] = []
    seen: set[int] = set()
    lines = (text or "").split("\n")
    i = 0
    while i < len(lines):
        if not _SOURCES_LEADIN_RE.match(lines[i]):
            i += 1
            continue
        j = i + 1
        while j < len(lines):
            line = lines[j]
            if not line.strip():
                j += 1
                continue
            m = _SOURCE_ROW_RE.match(line)
            if m is None:
                break
            n = int(m.group(1))
            if n not in seen:
                seen.add(n)
                pairs.append((n, m.group(2)))
            j += 1
        i = j
    return pairs


def citation_pairs(meta: dict[str, Any] | None, body: str) -> list[tuple[int, str]]:
    """The ``(marker, url)`` list for a cached perplexity ref.

    ``cache_state.meta['citation_map']`` (stamped at fetch — the
    id-spaced ``search_results`` numbering, gr542060) wins; rows without
    it fall back to parsing the stored ``Sources:`` block.
    """
    raw = (meta or {}).get("citation_map")
    if isinstance(raw, dict) and raw:
        pairs: list[tuple[int, str]] = []
        for k, v in raw.items():
            try:
                n = int(k)
            except (TypeError, ValueError):
                continue
            if isinstance(v, str) and v.strip():
                pairs.append((n, v.strip()))
        if pairs:
            return sorted(pairs)
    return parse_numbered_sources(body)


def materialize_web_refs(
    store: Store,
    pairs: list[tuple[int, str]],
    *,
    origin: str,
    create: bool = True,
) -> dict[int, int]:
    """Find-or-mint a ``web`` ref per cited URL; return ``{marker: ref_id}``.

    Dedup is the ``(provider='web', request_hash=sha256(canonical url))``
    cache key — the same hash ``get(kind='web', id=URL)`` derives, so a
    later fetch refreshes the stub row in place instead of minting a
    second ref. ``create=False`` resolves existing rows only (backfill
    dry-run). No network anywhere: minted rows are born-stale stubs.
    """
    mapping: dict[int, int] = {}
    for n, raw_url in pairs:
        key = canonical_web_key(raw_url)
        if key is None:
            continue
        request_hash = hashlib.sha256(key.encode("utf-8")).hexdigest()
        hit = store.get_cache_entry(provider="web", request_hash=request_hash)
        if hit is not None:
            mapping[n] = hit[0].id
            continue
        if not create:
            continue
        ref_id = _mint_stub(store, key=key, request_hash=request_hash, origin=origin)
        if ref_id is not None:
            mapping[n] = ref_id
    return mapping


def rewrite_citation_markers(text: str, mapping: dict[int, int]) -> str:
    """Rewrite ``[N]`` markers to ``[web:<ref_id>]`` for mapped numbers.

    Unmapped numbers (a citation the URL refused, a ``[N]`` that isn't a
    citation at all) pass through verbatim.
    """
    if not mapping or not text:
        return text

    def _sub(m: re.Match[str]) -> str:
        ref_id = mapping.get(int(m.group(1)))
        return f"[web:{ref_id}]" if ref_id is not None else m.group(0)

    return _MARKER_RE.sub(_sub, text)


def canonical_web_key(raw_url: str) -> str | None:
    """Canonical http(s) form of a cited URL, or ``None`` to skip it."""
    try:
        key = canonical_url(raw_url)
    except ValueError:
        return None
    return key if is_http_url(key) else None


def _mint_stub(store: Store, *, key: str, request_hash: str, origin: str) -> int | None:
    """Insert the born-stale ``web`` cache row for ``key``; return ref id.

    ``put_cache_entry`` DELETES a live ref under the same ``(kind, slug)``
    — a different URL that truncated to the same slug must not be
    clobbered, so a taken slug gets the ``-<sha8>`` disambiguator the
    perplexity slugs use. ``None`` when the slug space can't be taken
    cleanly (collision on the disambiguated form, or a measures-holding
    row — ``put_cache_entry`` refuses those with ``BadInput``).
    """
    slug = slug_from_url(key) or "web-fetch"
    # ``-q-<sha256(query)[:8]>`` — the query-tail form
    # ``WebHandler._slug_for`` mints (gr663681). Without it a cited
    # ``…?ids=111`` stub held the bare path's slug and its stored URL
    # then refused a bare-path fetch through ``_guard_slug_collision``.
    # Repo ``?ref=`` pins get the plain ``-q-`` tail here rather than
    # the handler's readable ``-ref-<ref>-<hash>`` — cosmetic only:
    # refetch resolves the stub by ``request_hash``, not by slug.
    query = urlsplit(key).query
    if query:
        slug = f"{slug}-q-{hashlib.sha256(query.encode('utf-8')).hexdigest()[:8]}"
    taken = store.get_cache_entry_by_slug(kind="web", slug=slug)
    if taken is not None:
        if taken[1].request_hash == request_hash:
            return taken[0].id
        slug = f"{slug}-{request_hash[:8]}"
        alt = store.get_cache_entry_by_slug(kind="web", slug=slug)
        if alt is not None:
            return alt[0].id if alt[1].request_hash == request_hash else None
    stub = ChunkInsert(
        ord=0,
        text=(
            f"(citation registered from {origin} — page not fetched yet; "
            f"get(kind='web', id={key!r}) fetches and caches it here)"
        ),
    )
    try:
        ref, _cache = store.put_cache_entry(
            kind="web",
            slug=slug,
            title=host_of(key) or key,
            body_blocks=[stub],
            provider="web",
            request_hash=request_hash,
            ttl_seconds=0,  # born-stale: next get re-fetches in place
            cache_meta={
                "url": key,
                "host": host_of(key),
                "stub": True,
                "stub_from": origin,
            },
        )
    except BadInput as exc:
        log.warning("web citation stub for %s refused: %s", key, exc)
        return None
    return ref.id


__all__ = [
    "canonical_web_key",
    "citation_pairs",
    "materialize_web_refs",
    "parse_numbered_sources",
    "rewrite_citation_markers",
]
