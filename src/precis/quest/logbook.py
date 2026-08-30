"""Quest logbook vocabulary + the shared append path.

The logbook is the quest's append-only, WORM, dated ledger — ``quest_log``
chunks off the quest ref (migration 0065, the ``gripe`` body+comment
pattern). Entries carry ``entry_type``+``by`` (+ optional ``cost``/
``chars``) in ``chunk.meta``. A ``milestone`` is a deed; a ``cost`` entry
feeds the tote, metered in ``chars`` (gr162594: ``cost_usd`` is null on the
free/quota-bound quest-tick lane).

:class:`~precis.handlers.quest.QuestHandler` and the autonomous
``quest_tick`` both write through :func:`append_entry` — one insert path,
one vocabulary.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from precis.store.types import BlockInsert

if TYPE_CHECKING:
    from precis.store import Store

#: The append-only logbook chunk_kind (seeded by migration 0065).
LOG_KIND = "quest_log"

#: Lightly-typed logbook entry vocabulary (``quest-layer`` (git-only)). A
#: ``milestone`` is a deed; a ``cost`` entry (or any entry with ``meta.cost``)
#: feeds the tote; a ``dead-end`` records what failed so the system stops
#: re-treading it; an un-answered ``hypothesis`` is a gap (slice 3).
ENTRY_TYPES: frozenset[str] = frozenset(
    {
        "note",
        "observation",
        "hypothesis",
        "result",
        "decision",
        "dead-end",
        "milestone",
        "reflection",
        "cost",
    }
)
DEFAULT_ENTRY = "note"

#: Who authored a logbook entry.
BY_VALUES: frozenset[str] = frozenset({"human", "agent", "dream", "system"})
DEFAULT_BY = "human"

#: ``by`` stamp for a system-measured fact (a converged relax, a harvested
#: autocatpath barrier, a ruled-out verdict) — never the model's own "agent"
#: attribution, so a measured result can't read as indistinguishable from
#: model narration (gr171148/171149).
MEASURED_BY = "system"


def append_entry(
    store: Store,
    quest_id: int,
    *,
    text: str,
    entry_type: str,
    by: str,
    cost: float | None = None,
    chars: int | None = None,
    extra_meta: dict[str, Any] | None = None,
) -> int:
    """Append one logbook entry; return its 1-based entry number.

    Validating ``entry_type``/``by`` against :data:`ENTRY_TYPES`/
    :data:`BY_VALUES` is the caller's job (the handler does, to surface a
    typo); this function is permissive — stamps whatever it's given, so the
    autonomous tick can clamp-and-proceed rather than raise. ``extra_meta``
    merges additional structured facts onto the entry's ``meta`` (e.g. the
    narrative growth-ratchet gate's word counts + reason) so they're
    minable later without parsing ``text`` — never overwrites the keys this
    function itself stamps (``chunk_kind``/``entry_type``/``by``/``cost``/
    ``chars``).
    """
    entry_meta: dict[str, Any] = {
        "chunk_kind": LOG_KIND,
        "entry_type": entry_type,
        "by": by,
    }
    if cost is not None:
        entry_meta["cost"] = float(cost)
    if chars is not None:
        entry_meta["chars"] = int(chars)
    if extra_meta:
        for k, v in extra_meta.items():
            entry_meta.setdefault(k, v)
    # Next pos = current chunk count. list_blocks_for_ref excludes the synthetic
    # card (ord=-1), so the first logbook entry lands at pos=0.
    next_pos = len(store.blocks.list_blocks_for_ref(quest_id))
    with store.tx() as conn:
        store.blocks.insert_blocks(
            quest_id,
            [BlockInsert(pos=next_pos, text=text, meta=entry_meta)],
            conn=conn,
        )
    return next_pos + 1


def clamp_entry_type(value: str | None) -> str:
    """Coerce an arbitrary (e.g. model-authored) entry type into the vocab."""
    v = (value or DEFAULT_ENTRY).strip().lower()
    return v if v in ENTRY_TYPES else DEFAULT_ENTRY


__all__ = [
    "BY_VALUES",
    "DEFAULT_BY",
    "DEFAULT_ENTRY",
    "ENTRY_TYPES",
    "LOG_KIND",
    "MEASURED_BY",
    "append_entry",
    "clamp_entry_type",
]
