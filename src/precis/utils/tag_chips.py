"""Rank-ordered tag chips for row and header surfaces (gr520321).

Ruling (gripe comment 1): a row shows *every* tag when the ref carries
four or fewer; above four it shows the top four by axis rank then
``+N more``: ``[STATUS:done] [planning] [pcb-dogfood] +2 more``. Order
comes from ``tag_axes.display_rank`` (migration 0256) — read once per
process, cached per store like ``eye_render._RELATION_READING``.
Free-form tags (the ``OPEN``/``FLAG`` sentinel namespaces,
``_tags_ops``) share the ``OPEN`` slot; an axis with no ``tag_axes``
row sorts at :data:`UNLISTED_RANK` — after free-form, before the
system tail — *unless* its namespace is all-uppercase, which reads as
pipeline bookkeeping (``*CASCADE``, ``INBOUND``) and sinks past the
seeded tail instead (gr524226). Within one slot, chips sort by their
rendered text.

The ruling's one exception to "anything goes" — a tag that only
restates the context is dropped *before* counting: ``STATUS:open`` on
an open-by-default kind (:data:`OPEN_BY_DEFAULT`), and a ``SPACE:``
value equal to the ring filter the caller already applied.

A chip render must never break a row: the bulk read and the axis read
are wrapped, failures read as "no chips".
"""

from __future__ import annotations

import logging
import weakref
from collections.abc import Mapping
from typing import Any

log = logging.getLogger(__name__)

#: Chips a neighbour row / search hit shows before ``+N more``.
ROW_CHIP_CAP = 4
#: Chips the focus ref's own eye header shows — it renders once.
HEAD_CHIP_CAP = 8

#: Sort slot for an axis with no ``tag_axes`` row: after free-form
#: (``OPEN`` seeds at 4), before the system tail (``SRC`` at 5).
UNLISTED_RANK = 4.5

#: Namespaces that ARE the value — the ``OPEN``/``FLAG`` sentinels carry
#: free-form text, so ``[OPEN:planning]`` would repeat the axis.
_BARE_NAMESPACES: frozenset[str] = frozenset({"OPEN", "FLAG"})

#: Kinds whose refs are open unless stamped otherwise — ``STATUS:open``
#: on them restates the default and drops before counting (todo and
#: gripe both stamp ``STATUS:open`` on create).
OPEN_BY_DEFAULT: frozenset[str] = frozenset({"todo", "gripe"})

#: ``(namespace, value, kind)`` quiet triples — a tag that restates its
#: kind's default drops before counting, like ``STATUS:open``.
#: ``project:precis`` on a gripe IS the default (gr543639); only a
#: non-default project is worth a chip.
_QUIET_DEFAULTS: frozenset[tuple[str, str, str]] = frozenset(
    {("OPEN", "project:precis", "gripe")}
)

#: Per-store cache for :func:`_axis_ranks`, dropped with its store.
_AXIS_RANKS: weakref.WeakKeyDictionary[Any, dict[str, float]] = (
    weakref.WeakKeyDictionary()
)


def _axis_ranks(store: Any) -> dict[str, float]:
    """``{namespace: display_rank}`` from ``tag_axes``, cached per store
    for its lifetime — the registry is static once migrations have run
    ("read once per process"). A failed read is *not* cached: a process
    up before the migration lands retries next call rather than pinning
    the degraded order for the store's lifetime."""
    cached = _AXIS_RANKS.get(store)
    if cached is not None:
        return cached
    try:
        with store.pool.connection() as conn:
            rows = conn.execute(
                "SELECT namespace, display_rank FROM tag_axes"
            ).fetchall()
        ranks = {str(ns): float(r) for ns, r in rows}
    except Exception:
        # The axes table is an ordering hint, never a render gate.
        log.debug("tag_axes read failed", exc_info=True)
        return {}
    _AXIS_RANKS[store] = ranks
    return ranks


def _rank(ns: str, ranks: Mapping[str, float]) -> float:
    if ns in _BARE_NAMESPACES:
        return ranks.get("OPEN", 4.0)
    if ns in ranks:
        return ranks[ns]
    if ns.isupper():
        # An unlisted all-uppercase axis is pipeline bookkeeping
        # (``*CASCADE``, ``INBOUND``, … — gr524226): it sinks past the
        # seeded tail rather than leading the row ahead of the
        # categorizer axes, so a new pipeline axis never needs a
        # migration just to stay quiet.
        return max(ranks.values(), default=UNLISTED_RANK) + 1
    return UNLISTED_RANK


def _chip_text(ns: str, value: str) -> str:
    return value if ns in _BARE_NAMESPACES else f"{ns}:{value}"


def chips_for(
    pairs: list[tuple[str, str]],
    *,
    kind: str,
    ranks: Mapping[str, float],
    ring_spaces: frozenset[str] | None = None,
    cap: int = ROW_CHIP_CAP,
) -> str:
    """``[a] [b] [c] [d] +N more`` for one ref's ``(namespace, value)``
    pairs — the drops applied, then rank-sorted and capped."""
    kept = [
        (ns, v)
        for ns, v in pairs
        if not (
            (ns == "STATUS" and v == "open" and kind in OPEN_BY_DEFAULT)
            or (ns == "SPACE" and ring_spaces is not None and v in ring_spaces)
            or (ns, v, kind) in _QUIET_DEFAULTS
        )
    ]
    if not kept:
        return ""
    keyed = sorted(kept, key=lambda p: (_rank(p[0], ranks), _chip_text(*p)))
    shown = [_chip_text(ns, v) for ns, v in keyed[:cap]]
    more = len(kept) - len(shown)
    out = " ".join(f"[{t}]" for t in shown)
    return f"{out} +{more} more" if more else out


def chips_bulk(
    store: Any,
    ids_kinds: Mapping[int, str],
    *,
    ring_spaces: frozenset[str] | None = None,
    cap: int = ROW_CHIP_CAP,
    tagged: dict[int, list[tuple[str, str]]] | None = None,
) -> dict[int, str]:
    """``{ref id: chips}`` for many refs in one ``ref_tags_bulk`` read —
    the N+1 avoidance for a page of rows. ``ids_kinds`` maps each ref id
    to its kind (the ``STATUS:open`` drop is kind-dependent). ``tagged``
    lets a caller that already bulk-read pass its result instead of a
    second query. A failed bulk read reads as no chips, never as a
    broken render."""
    if not ids_kinds:
        return {}
    if tagged is None:
        try:
            tagged = store.ref_tags_bulk(list(ids_kinds))
        except Exception:
            log.debug("tag chips bulk read failed", exc_info=True)
            return {}
    if not tagged:
        return {}
    ranks = _axis_ranks(store)
    return {
        rid: c
        for rid, pairs in tagged.items()
        if (
            c := chips_for(
                pairs,
                kind=ids_kinds.get(rid, ""),
                ranks=ranks,
                ring_spaces=ring_spaces,
                cap=cap,
            )
        )
    }


__all__ = [
    "HEAD_CHIP_CAP",
    "OPEN_BY_DEFAULT",
    "ROW_CHIP_CAP",
    "UNLISTED_RANK",
    "chips_bulk",
    "chips_for",
]
