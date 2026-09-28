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

__all__ = ["quest_body_marker", "resolved_quest_body"]


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
