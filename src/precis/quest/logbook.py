"""Quest logbook vocabulary + the shared append path.

The logbook is the quest's append-only, WORM, dated ledger — ``quest_log``
chunks hanging off the quest ref (the ``gripe`` body+comment pattern, migration
0065). Lightly-typed entries carry ``entry_type`` + ``by`` (+ optional ``cost``)
in ``chunk.meta``. A ``milestone`` is a deed; a ``cost`` entry feeds the tote.

Both the :class:`~precis.handlers.quest.QuestHandler` (the ``put(id=N, text=…,
entry=…)`` idiom) and the autonomous ``quest_tick`` (slice 4) write through
:func:`append_entry`, so there is exactly one insert path and one vocabulary.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from precis.store.types import BlockInsert

if TYPE_CHECKING:
    from precis.store import Store

#: The append-only logbook chunk_kind (seeded by migration 0065).
LOG_KIND = "quest_log"

#: Lightly-typed logbook entry vocabulary (docs/proposals/quest-layer.md). A
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
BY_VALUES: frozenset[str] = frozenset({"human", "agent", "dream"})
DEFAULT_BY = "human"


def append_entry(
    store: Store,
    quest_id: int,
    *,
    text: str,
    entry_type: str,
    by: str,
    cost: float | None = None,
) -> int:
    """Append one logbook entry; return its 1-based entry number.

    The caller is responsible for validating ``entry_type`` / ``by`` against
    :data:`ENTRY_TYPES` / :data:`BY_VALUES` when it wants to *reject* bad input
    (the handler does, to surface a typo). This function is permissive — it
    stamps whatever it is given — so the autonomous tick can clamp-and-proceed
    rather than raise.
    """
    entry_meta: dict[str, Any] = {
        "chunk_kind": LOG_KIND,
        "entry_type": entry_type,
        "by": by,
    }
    if cost is not None:
        entry_meta["cost"] = float(cost)
    # Next pos = current chunk count. list_blocks_for_ref excludes the synthetic
    # card (ord=-1), so the first logbook entry lands at pos=0.
    next_pos = len(store.list_blocks_for_ref(quest_id))
    with store.tx() as conn:
        store.insert_blocks(
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
    "append_entry",
    "clamp_entry_type",
]
