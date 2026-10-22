"""The one place that reads a quest's ``meta.quest_body`` marker.

Three callers tick a quest: the autonomous coordinator
(:mod:`precis.workers.job_types.quest_tick`), the manual CLI driver
(``precis quest tick`` / ``precis quest run`` → :mod:`precis.cli.quest`),
and the allocator's one-shot (:func:`precis.quest.allocator.run_allocator_pass`).
All three must resolve the SAME marker the same way — a second (or third)
copy of the read is exactly how a manual entry point silently ticks an
``inquiry``-marked quest as ``materials`` (docs/backlog/quest-bodies-inquiry.md;
the bug this module fixes). The coordinator's own :func:`~precis.workers.
job_types.quest_tick._quest_body` now delegates to :func:`quest_body_marker`
here rather than duplicating the read.

Unset always normalises to :data:`~precis.quest.weave_tick.QUEST_BODY_MATERIALS`
— never inferred from ``reaction_config``/``compute_lane``/``rubric_objectives``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from precis.quest.weave_tick import QUEST_BODY_MATERIALS, QUEST_BODY_META_KEY

if TYPE_CHECKING:
    from precis.store.protocols import RefMetaStore

__all__ = [
    "LIT_LANE_META_KEY",
    "quest_body_marker",
    "quest_lit_enabled",
    "resolved_quest_body",
]

#: Quest meta key holding the lit-search switch. ``"off"`` skips the tick's
#: lit-search stage (and the quiet-tick acquire fallback); any other value —
#: or the key's absence — keeps lit-search on. The lit lane is deliberately
#: NOT ``meta.compute_lane``: lit-search is network (S2 + OA fetch), not GPU
#: compute, so gating it on the compute lane starved lit-only quests
#: (gr500282 — qu500066 skipped four consecutive ticks of searches).
LIT_LANE_META_KEY = "lit_lane"


def quest_body_marker(store: RefMetaStore, quest_id: int) -> str | None:
    """The quest's raw ``meta.quest_body`` marker, or ``None`` if unset.

    Defensive: a missing/exception-raising ``get_ref`` (the common case in
    unit tests that don't stub it) degrades to ``None``, not a crash — same
    shape as ``_quest_status``/``_quest_compute_enabled``. Callers that want
    today's-behaviour-by-default should use :func:`resolved_quest_body`
    instead of normalising ``None`` themselves.
    """
    try:
        ref = store.get_ref(kind="quest", id=quest_id)
    except Exception:
        return None
    if ref is None:
        return None
    meta = ref.meta or {}
    val = meta.get(QUEST_BODY_META_KEY)
    return str(val) if val is not None else None


def resolved_quest_body(store: RefMetaStore, quest_id: int) -> str:
    """:func:`quest_body_marker`, normalised to
    :data:`~precis.quest.weave_tick.QUEST_BODY_MATERIALS` when unset."""
    return quest_body_marker(store, quest_id) or QUEST_BODY_MATERIALS


def quest_lit_enabled(store: RefMetaStore, quest_id: int) -> bool:
    """Whether this quest's ticks may run lit-search work.

    ``meta.lit_lane == "off"`` disables the lane; everything else —
    including a missing/exception-raising ``get_ref`` (the common case in
    unit tests that don't stub it) — degrades to the lit-on default, not a
    crash. Read fresh each call, so flipping the key takes effect on the
    next tick without bouncing the coordinator. Same read shape as
    :func:`quest_body_marker` /
    :func:`precis.workers.job_types.quest_tick._quest_compute_enabled`.
    """
    try:
        ref = store.get_ref(kind="quest", id=quest_id)
    except Exception:
        return True
    if ref is None:
        return True
    meta = ref.meta or {}
    return str(meta.get(LIT_LANE_META_KEY) or "") != "off"
