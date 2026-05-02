"""Shared render + search helpers for slug-addressed ref kinds.

Slug-addressed kinds (``oracle``, ``conv``, ``quest``, plus slices of
``paper`` / ``patent``) repeatedly hand-roll the same four pieces:

1. A ref-level lexical search that calls ``search_refs_lexical`` +
   ``count_refs_lexical`` and renders ``# N match(es) for 'q'``
   followed by one ``## {kind} {slug}  (rank=X.XX)`` block per hit.
2. A ``search_hits`` variant that returns :class:`SearchHit` records
   for cross-kind merge.
3. A bare ``# {N} {kind}(s)`` list view rendered from
   ``list_refs(kind=..., limit=50)``.
4. A "coerce (kind, slug) → live :class:`Ref` or raise ``NotFound``"
   step that every ``get`` / ``tag`` / ``link`` / ``delete`` entry
   point repeats — ``resolve_live_slug_ref`` is that step.

These helpers factor each piece into a free-standing function so the
per-handler methods become thin wrappers. Kept free-standing (no
base class) to match the style set by :mod:`_link_tag_ops` —
"call sites stay obvious" was the original rationale and still holds.

Per-kind hints (``next:`` trailers on empty states, custom noun
phrasing for ``format_search_headline``) are passed in rather than
hard-coded, so handlers remain free to tailor the agent-facing
wording without forking the helper.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from precis.errors import BadInput, NotFound
from precis.response import Response
from precis.utils.next_block import render_next_section
from precis.utils.search_header import format_search_headline
from precis.utils.search_merge import SearchHit, ref_hits_to_search_hits

if TYPE_CHECKING:
    from precis.store import Ref, Store


def search_slug_refs(
    store: Store,
    *,
    kind: str,
    q: str | None,
    top_k: int,
    noun: str,
    empty_next: list[tuple[str, str]] | None = None,
) -> Response:
    """Render a ref-level lexical search over a slug-addressed kind.

    ``q`` must be non-empty; empty queries raise :class:`BadInput`
    with a hint tailored to ``kind`` so the agent sees a concrete
    recovery call. ``noun`` is the singular form used in the
    headline (``"oracle match"``, ``"quest match"``); it's
    pluralised by :func:`format_search_headline`.

    ``empty_next`` is an optional list of ``(call, description)``
    pairs appended as a ``Next:`` trailer to the empty-state body.
    Without it, the empty body is just ``"no <kind> entries match
    'q'"``.
    """
    if q is None or not q.strip():
        raise BadInput(
            "search requires q=",
            next=f"search(kind={kind!r}, q='your query')",
        )

    hits = store.search_refs_lexical(q=q, kind=kind, limit=top_k)
    if not hits:
        body = f"no {kind} entries match {q!r}"
        if empty_next:
            body += render_next_section(empty_next)
        return Response(body=body)

    total = store.count_refs_lexical(q=q, kind=kind)
    lines = [
        format_search_headline(
            n_returned=len(hits),
            total=total,
            noun=noun,
            query=q,
        )
    ]
    for ref, rank in hits:
        preview = (ref.title[:140] + "…") if len(ref.title) > 140 else ref.title
        lines.append(f"\n## {kind} {ref.slug}  (rank={rank:.2f})\n{preview}")
    return Response(body="\n".join(lines))


def search_hits_slug_refs(
    store: Store,
    *,
    kind: str,
    q: str | None,
    top_k: int,
) -> list[SearchHit]:
    """Ref-level lexical search returned as :class:`SearchHit`\\ s.

    Mirrors :func:`search_slug_refs`'s retrieval path but skips the
    rendering — used by the cross-kind merge (``kind='*'`` /
    comma-lists). Empty queries yield an empty list rather than
    raising, because the cross-kind dispatcher asks each kind for
    hits and expects failure-free streams.
    """
    if not (q and q.strip()):
        return []
    pairs = store.search_refs_lexical(q=q, kind=kind, limit=top_k)
    return ref_hits_to_search_hits(pairs, kind=kind)


def render_slug_ref_list(
    store: Store,
    *,
    kind: str,
    label_plural: str,
    limit: int = 50,
    empty_body: str | None = None,
    empty_next: list[tuple[str, str]] | None = None,
    populated_next: list[tuple[str, str]] | None = None,
    preview_len: int = 80,
    slug_col_width: int = 30,
) -> Response:
    """Render a plain ``# N kind(s)`` list for a slug-addressed kind.

    Used by the browse-all path (``get(kind=<slug-kind>)`` with no
    id=): shows the most-recently-updated refs of ``kind`` in a
    single-column slug/title listing.

    Args:
        label_plural: Phrasing for the headline. The legacy
            ``"foo(s)"`` template (e.g. ``"oracle(s)"``,
            ``"conversation(s)"``) is auto-resolved to the
            count-correct form here — ``# 1 oracle`` /
            ``# 9 oracles`` rather than the ungrammatical
            ``# 9 oracle(s)`` the MCP critic flagged 2026-05-02.
            Already-resolved labels (``"quest"``,
            ``"conversations"``) pass through verbatim.
        limit: Row cap on ``store.list_refs``. Defaults to 50,
            which matches the existing oracle/conv values.
        empty_body: Override message shown when the corpus has no
            refs of this kind yet. Defaults to
            ``"no <kind> entries yet"``.
        empty_next: Optional ``Next:`` trailer on the empty path
            so the agent has somewhere to go from a blank list.
        populated_next: Optional ``Next:`` trailer on the
            non-empty path. Brings list views in line with every
            other ``/recent`` shape that already teaches the next
            call shape (MCP critic MINOR-C 2026-05-02 — oracle was
            the only ``/recent`` view shipping without one).

    The resulting body is ASCII-aligned with a fixed slug column
    so tall listings stay readable at monospaced widths.
    """
    refs = store.list_refs(kind=kind, limit=limit)
    if not refs:
        body = empty_body or f"no {kind} entries yet"
        if empty_next:
            body += render_next_section(empty_next)
        return Response(body=body)

    headline_label = _resolve_count_plural(label_plural, n=len(refs))
    lines = [f"# {len(refs)} {headline_label}"]
    for r in refs:
        preview = (
            (r.title[:preview_len] + "…") if len(r.title) > preview_len else r.title
        )
        slug = r.slug or "?"
        if len(slug) > slug_col_width:
            slug = slug[: slug_col_width - 1] + "…"
        lines.append(f"  {slug:<{slug_col_width}}  {preview}")
    body = "\n".join(lines)
    if populated_next:
        body += render_next_section(populated_next)
    return Response(body=body)


def _resolve_count_plural(label: str, *, n: int) -> str:
    """Resolve a count-aware label.

    The legacy ``"foo(s)"`` template is replaced with ``""`` for
    ``n == 1`` and ``"s"`` for ``n != 1`` so the headline is
    grammatical at any cardinality. Labels without the
    parenthetical are returned verbatim — the caller signalled
    that they own pluralisation explicitly.
    """
    if "(s)" in label:
        return label.replace("(s)", "" if n == 1 else "s")
    return label


def resolve_live_slug_ref(
    store: Store,
    *,
    kind: str,
    id: str | int,
    next_hint: str | None = None,
    options: Sequence[str] | None = None,
) -> Ref:
    """Coerce a ``(kind, slug)`` pair to a live :class:`Ref` or raise.

    Canonicalises the pattern that every slug-addressed handler
    (quest, oracle, paper, patent, conv, markdown, plaintext)
    repeats in its ``get`` / ``tag`` / ``link`` / ``delete`` entry
    points::

        ref = store.get_ref(kind=kind, id=slug)
        if ref is None:
            raise NotFound(f"{kind} slug {slug!r} not found", next=...)

    Returns the live :class:`Ref` (``.id``, ``.slug``, ``.title``,
    ``.meta``, …) so callers destructure whatever they need. Note
    that :meth:`Store.get_ref` already filters out soft-deleted
    rows, so the returned ref is always live — callers handling
    tombstones should use the lower-level store API directly.

    Args:
        store: Store instance to probe.
        kind: Kind slug, used both for the DB lookup and to shape
              the default error message.
        id:   The slug the caller passed in. Coerced to :class:`str`
              and stripped of surrounding whitespace.
        next_hint: Optional override for the error's ``next:``
              hint. Defaults to the canonical
              ``search(kind=..., q='...') to find existing``.
              Pass a richer hint when the handler already has a
              concrete path (``_suggest_paper_slugs``-style fuzzy
              matches belong in ``options=`` instead).
        options: Optional spelling suggestions forwarded into the
              :class:`NotFound` envelope so the agent sees
              ``options: [...]`` in the error body. Falsy values
              (empty list / ``None``) are normalised to ``None``
              so the envelope stays tidy.

    Raises:
        NotFound: the ref does not exist (or was soft-deleted and
                  is therefore unreachable through ``get_ref``).
    """
    slug = str(id).strip()
    ref = store.get_ref(kind=kind, id=slug)
    if ref is None:
        raise NotFound(
            f"{kind} slug {slug!r} not found",
            next=next_hint or f"search(kind={kind!r}, q='...') to find existing",
            options=list(options) if options else None,
        )
    return ref


__all__ = [
    "render_slug_ref_list",
    "resolve_live_slug_ref",
    "search_hits_slug_refs",
    "search_slug_refs",
]
