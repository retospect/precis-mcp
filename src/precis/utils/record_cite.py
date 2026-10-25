"""Whole-record cite support — the ``record_type`` vocabulary and the one
live predicate the declare / lint / edge-stamp / export paths share
(gr514384, docs/backlog/whole-record-cite.md).

A *record* is a ``paper`` ref typed ``meta.record_type`` ``'software'``
or ``'dataset'`` that carries **no body chunks** — a citeable source
with no passage to quote (a Zenodo software release, a dataset landing
page). Its whole-ref ``[pa<id>]`` cite is therefore legitimate by
construction, not merely tolerated: the whole-paper lint hint and the
hygiene list skip it, ``sync_draft_links`` stamps its ``cites`` edge
``cite_mode='record'``, and the exporters emit ``@software`` /
``@dataset`` instead of a venue-less ``@article``.

Eligibility is a *live* predicate over current state, never a cached
flag — a record that gains a body (a README PDF lands) stops matching
and its cites revert to the ordinary whole-paper rules; a paper with
body chunks can never match, so ``put(record_type=…)`` refuses it and
the exemption cannot be set on real papers.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from precis.store.store import Store

#: ``meta.record_type`` values that make a paper a whole-record cite.
#: Software and dataset only — every other value is refused at declare
#: time (gr514384: "we allow this for @software and @dataset, not normal
#: papers").
RECORD_TYPES: frozenset[str] = frozenset({"software", "dataset"})


def record_type_of(meta: Any) -> str:
    """The canonical ``meta.record_type`` of a ref, or ``""``.

    Tolerates ``None`` / non-dict meta and non-string values; only the
    two declared :data:`RECORD_TYPES` read back — a stale or
    out-of-vocabulary value counts as untyped everywhere downstream.
    """
    rt = str((meta or {}).get("record_type") or "").strip().lower()
    return rt if rt in RECORD_TYPES else ""


def is_record_cite(store: Store, ref: Any) -> bool:
    """The live predicate: a paper typed ``record_type`` carrying zero
    body chunks (``ord >= 0``) — the ``[pa<id>]`` whole-ref cite is the
    *only* cite the record can take, so it is exempt from the
    whole-paper nudges by construction."""
    if ref is None or getattr(ref, "kind", None) != "paper":
        return False
    if not record_type_of(getattr(ref, "meta", None)):
        return False
    return int(ref.id) not in store.refs_with_body_chunks([int(ref.id)])


def record_cite_handles(store: Store, handles: Iterable[str]) -> set[str]:
    """Batched :func:`is_record_cite` for the lint/hygiene paths: which of
    the bare ``[pa<id>]`` handle tokens in ``handles`` name a live record
    cite, returned as a set of the same token strings.

    One parse pass (no query — a ``pa`` handle's body *is* the ref id),
    one ``fetch_refs_by_ids``, one ``refs_with_body_chunks``: the draft
    write path stays cheap regardless of how many ``[pa<id>]`` a chunk
    carries.
    """
    from precis.utils import handle_registry

    handles = list(handles)
    candidates: set[int] = set()
    for h in handles:
        parsed = handle_registry.parse(h)
        if parsed is not None and parsed[0] == "paper" and not parsed[1]:
            candidates.add(parsed[2])
    if not candidates:
        return set()
    refs = store.fetch_refs_by_ids(sorted(candidates), include_deleted=False)
    typed = {
        rid for rid, ref in refs.items() if record_type_of(getattr(ref, "meta", None))
    }
    records = typed - store.refs_with_body_chunks(sorted(typed))
    if not records:
        return set()
    return {
        h
        for h in handles
        if (p := handle_registry.parse(h)) is not None
        and p[0] == "paper"
        and not p[1]
        and p[2] in records
    }


__all__ = ["RECORD_TYPES", "is_record_cite", "record_cite_handles", "record_type_of"]
