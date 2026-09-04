"""Cite-closure resolver for ``exclude=`` — shared by ``search(kind='paper')``
and ``get(kind='semanticscholar', ...)``.

docs/backlog/discovery-exclude-by-container.md: an ``exclude=`` entry may be,
mixed in one list, a bare paper slug/id/DOI (today's paper-search behavior —
delegated to :func:`precis.handlers._paper_search._normalise_exclude_slug`,
silently dropped when stale), a draft ref (``dr…`` — the WHOLE draft), or a
draft chunk handle (``dc…`` — that chunk's hierarchical SUBTREE, via
``Store.drafts.draft_subtree_chunk_ids``). The two container forms resolve
to every paper cited anywhere within their chunk text: a ``[pa…]`` cite
token directly, a ``[pc…]`` token via its owning paper
(``Store.resolve_handle``), a ``[fi…]`` token — when the finding is a live
Taproot claim hub — via the hub's grounding/supporter papers
(``originators`` + ``corroborators``, resolved in bulk via
:func:`precis.taproot.seniority.is_claim_hub_bulk` +
:func:`~precis.taproot.seniority.derive_evidence_bulk` — a draft citing N
hubs costs a constant few queries, not O(N)): the draft already "knows"
those works through the hub's evidence even when it never names them
directly (DECIDED, backlog item's decisions log — revisit if this
over-excludes in practice). A ``[fi…]`` token pointing at a non-hub
finding contributes nothing (no evidence to walk).

Unlike the legacy bare-slug path (silent-drop on a stale slug — a caller's
skip-list may carry ids that no longer resolve, and the paper search would
rather quietly skip than fail the whole call), a *container* entry that
doesn't resolve (an unknown ``dr…`` / ``dc…``) is a caller-visible mistake —
they named a specific container expecting its cites to be excluded — so it
raises :class:`~precis.errors.BadInput` naming the offending entry.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from precis.errors import BadInput
from precis.utils import handle_registry
from precis.utils.mentions import BARE_BRACKET_REF_PATTERN

if TYPE_CHECKING:
    from precis.store.store import Store


def resolve_exclude_paper_ids(
    entries: list[str] | None, *, store: Store, kind: str = "paper"
) -> set[int]:
    """``exclude=`` (mixed paper-slug / ``dr…`` / ``dc…`` entries) → the
    set of paper ``ref_id``s to drop from a result set.

    ``entries=None`` / ``[]`` returns the empty set — the common, no-op
    case for both callers. ``kind=`` scopes the bare-slug leg only
    (``paper`` search's cfp/datasheet subclasses share this resolver but
    address their own kind's slugs); the two draft-container legs always
    resolve ``[pa…]``/``[pc…]`` cite tokens to ``paper`` refs regardless —
    a draft cites papers, never cfp/datasheet records.
    """
    if not entries:
        return set()
    # Local import: ``_paper_search`` is itself lazily imported from
    # ``paper.py`` (see that module's docstring) to dodge a circular
    # import; this module has a SECOND caller (``semanticscholar.py``)
    # with no dependency on ``paper.py`` at all, so importing at module
    # scope here would force that unrelated caller through the same
    # import-order hazard for no benefit.
    from precis.handlers._paper_search import _normalise_exclude_slug

    paper_ids: set[int] = set()
    bare_slugs: list[str] = []
    seen_slugs: set[str] = set()
    # Record-form universal handles (``pa<id>``, never chunk-form
    # ``pc<id>``) skip the per-item ``store.resolve_handle()`` round trip
    # entirely: the handle's decimal body IS the ref_id (no lookup
    # needed to find it), so every such entry across the whole
    # ``exclude=`` list resolves in ONE bulk ``fetch_refs_by_ids`` call
    # below instead of N sequential ``resolve_handle`` connections (the
    # gr311339 hang — a 4-entry exclude took >1800s where a 1-entry
    # exclude was fast). Chunk-form handles (``pc<id>``) and anything
    # that doesn't survive the bulk fetch (dead / superseded / kind
    # mismatch) still fall through to the original per-item
    # ``_normalise_exclude_slug`` path below — rare enough not to be
    # worth a second bulk shape, and it preserves that path's existing
    # supersede-follow / redirect-hint behavior exactly.
    record_handle_pks: dict[str, int] = {}
    other_entries: list[str] = []
    for raw in entries:
        entry = (raw or "").strip()
        if not entry:
            continue
        texts = _draft_container_texts(entry, store=store)
        if texts is not None:
            paper_ids |= _cite_closure_paper_ids(texts, store=store)
            continue
        parsed = handle_registry.parse(entry)
        if parsed is not None and not parsed[1] and parsed[0] == kind:
            record_handle_pks[entry] = parsed[2]
        else:
            other_entries.append(entry)

    if record_handle_pks:
        refs_map = store.fetch_refs_by_ids(list(set(record_handle_pks.values())))
        for entry, pk in record_handle_pks.items():
            ref = refs_map.get(pk)
            if ref is not None and ref.retired_at is None and ref.kind == kind:
                paper_ids.add(pk)
            else:
                # Dead / superseded / not found in the bulk fetch — fall
                # back to the original per-item resolution (handles the
                # merge-redirect / supersede-chain case correctly).
                other_entries.append(entry)

    for entry in other_entries:
        slug = _normalise_exclude_slug(entry, store=store)
        if slug is not None and slug not in seen_slugs:
            seen_slugs.add(slug)
            bare_slugs.append(slug)
    if bare_slugs:
        paper_ids.update(store.fetch_ref_ids_by_slugs(bare_slugs, kind=kind))
    return paper_ids


def _draft_container_texts(entry: str, *, store: Store) -> list[str] | None:
    """The chunk texts of the draft/draft-chunk container ``entry``
    addresses, or ``None`` when ``entry`` isn't a draft handle at all (the
    caller falls through to the legacy paper-slug path).

    Raises :class:`BadInput` when ``entry`` parses as a ``dr…`` / ``dc…``
    handle shape but doesn't resolve to a live draft/chunk — see the
    module docstring for why containers are strict where bare slugs
    aren't.
    """
    parsed = handle_registry.parse(entry)
    if parsed is None or parsed[0] != "draft":
        return None
    _kind, is_chunk, _pk = parsed
    if is_chunk:
        # Fetch the chunk ONCE (gives both its own id and its owning
        # ref_id) rather than calling ``draft_subtree_chunk_ids`` (which
        # internally re-fetches the same chunk row) and then fetching it
        # AGAIN ourselves just for ``ref_id`` — ``descendant_chunk_ids``
        # takes the already-known chunk_id straight to the subtree walk.
        chunk = store.drafts.get_draft_chunk(entry)
        if chunk is None:
            raise BadInput(
                f"exclude={entry!r} — no such draft chunk",
                next="check the dc<id> handle, or drop it from exclude=",
            )
        keep = {chunk.chunk_id, *store.drafts.descendant_chunk_ids(chunk.chunk_id)}
        return [
            c.text
            for c in store.drafts.reading_order(chunk.ref_id)
            if c.chunk_id in keep
        ]
    resolved = store.resolve_handle(entry)
    if resolved is None:
        raise BadInput(
            f"exclude={entry!r} — no such draft",
            next="check the dr<id> handle, or drop it from exclude=",
        )
    return [c.text for c in store.drafts.reading_order(resolved.ref_id)]


def _cite_closure_paper_ids(texts: list[str], *, store: Store) -> set[int]:
    """Every paper ``ref_id`` cited (directly, or via a paper chunk / claim
    hub) anywhere in ``texts`` — the ``[pa…]`` / ``[pc…]`` / ``[fi…]``
    bracket-handle grammar :data:`~precis.utils.mentions.
    BARE_BRACKET_REF_PATTERN` already defines for draft prose.
    """
    # Local import — the Taproot bulk hub-evidence machinery this module
    # otherwise has no reason to load (most drafts cite no hubs at all).
    from precis.taproot.seniority import derive_evidence_bulk, is_claim_hub_bulk

    ids: set[int] = set()
    seen: set[str] = set()
    paper_tokens: list[str] = []
    finding_ids: list[int] = []
    for text in texts:
        if not text:
            continue
        for m in BARE_BRACKET_REF_PATTERN.finditer(text):
            bare = m.group("bare")
            if bare in seen:
                continue
            seen.add(bare)
            parsed = handle_registry.parse(bare)
            if parsed is None:
                continue  # sigil form (¶/§) or unresolvable — not our grammar
            kind, _is_chunk, pk = parsed
            if kind == "paper":
                paper_tokens.append(bare)
            elif kind == "finding":
                finding_ids.append(pk)

    # [pa…]/[pc…] resolution — one ``resolve_handle`` round trip per
    # distinct token (deduped above); no bulk form exists for a mixed
    # record/chunk handle lookup.
    for bare in paper_tokens:
        resolved = store.resolve_handle(bare)
        if resolved is not None:
            ids.add(resolved.ref_id)

    # [fi…] hub expansion — BULK: one query to find which cited findings
    # are live claim hubs, one more to derive every hub's evidence at
    # once, instead of two queries PER token (a draft citing 20 hubs used
    # to issue ~80 queries here). Non-hub findings simply have no entry
    # in ``hub_flags`` truthy set — no evidence to walk.
    if finding_ids:
        hub_flags = is_claim_hub_bulk(store, finding_ids)
        hub_ids = [fid for fid in finding_ids if hub_flags.get(fid)]
        if hub_ids:
            evidence_by_hub = derive_evidence_bulk(store, hub_ids)
            for evidence in evidence_by_hub.values():
                ids.update(
                    edge.paper_ref_id
                    for edge in (*evidence.originators, *evidence.corroborators)
                )
    return ids


__all__ = ["resolve_exclude_paper_ids"]
