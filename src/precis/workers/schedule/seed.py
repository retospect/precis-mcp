"""Idempotent Watches-umbrella seed.

The Watches root is a single ``kind='todo'`` ref tagged
``level:recurring`` with ``meta.builtin='watches-root'``. Every
recurring lands under it by default; the umbrella itself is a folder
(``meta.schedule`` is null) so the spawner skips it on ticks.

Seeding is idempotent on ``meta->>'builtin' = 'watches-root'`` so this
function is safe to call from both:

* the schedule worker's first-run path (``run_schedule_pass`` calls
  ``ensure_watches_root`` before walking the recurring list);
* the todo handler at write time, when a ``level:recurring`` ref is
  created without an explicit ``parent_id`` — we need an id for the
  default-parent, and the worker may not have run yet.

Either way, the seed is the same INSERT + tag pair. The function
returns the umbrella's ``ref_id`` so callers can stash it in a
``parent_id`` field without a follow-up SELECT.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from precis.store.types import Tag

if TYPE_CHECKING:
    from precis.store import Store

#: Meta marker for the Watches umbrella. The handler's
#: ``check_not_builtin`` rejects ``delete`` on refs carrying any
#: ``meta.builtin`` value, so deleting Watches takes an explicit DB
#: write — the umbrella can't be lost to a stray agent call.
WATCHES_BUILTIN = "watches-root"
WATCHES_TITLE = "Watches"


def ensure_watches_root(store: Store) -> int:
    """Return the Watches umbrella's ``ref_id``, seeding if necessary.

    Looks up the seeded row by ``meta->>'builtin'=<WATCHES_BUILTIN>``.
    When missing, inserts a fresh ``todo`` ref + the ``level:recurring``
    tag in a single transaction so the umbrella always carries the
    gradient tag the doable filter walks.
    """
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT ref_id FROM refs "
            "WHERE kind='todo' AND deleted_at IS NULL "
            "AND meta->>'builtin' = %s LIMIT 1",
            (WATCHES_BUILTIN,),
        ).fetchone()
    if row is not None:
        return int(row[0])
    with store.tx() as conn:
        # Re-check inside the tx so two concurrent workers don't both
        # seed. The unique guarantee is paper-thin (no unique constraint
        # on meta->>'builtin'), but the second writer's SELECT inside
        # the same advisory-lock-protected window will see the first
        # writer's INSERT — losers retry safely.
        row = conn.execute(
            "SELECT ref_id FROM refs "
            "WHERE kind='todo' AND deleted_at IS NULL "
            "AND meta->>'builtin' = %s LIMIT 1 FOR UPDATE",
            (WATCHES_BUILTIN,),
        ).fetchone()
        if row is not None:
            return int(row[0])
        ref = store.insert_ref(
            kind="todo",
            slug=None,
            title=WATCHES_TITLE,
            meta={"builtin": WATCHES_BUILTIN},
            conn=conn,
        )
        # The umbrella carries the gradient tag but no schedule —
        # spawner checks ``meta.schedule is null`` to skip folders.
        store.add_tag(
            ref.id,
            Tag.open("level:recurring"),
            set_by="system",
            conn=conn,
        )
        # Track it as a structural event so the timeline shows the
        # umbrella's birth without the operator having to dig through
        # migrations to find when it appeared.
        store.append_event(
            ref.id,
            source="schedule",
            event="watches-root:seeded",
            conn=conn,
        )
        return int(ref.id)


__all__ = ["WATCHES_BUILTIN", "WATCHES_TITLE", "ensure_watches_root"]
