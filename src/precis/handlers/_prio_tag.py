"""``PRIO:`` closed-tag ↔ ``refs.prio`` column translation.

Shared by the handlers whose priority is a real sort column rather than a
decorative tag — the todo tree (migration 0014), quests
(:mod:`precis.handlers.quest`), and gripes (:mod:`precis.handlers.gripe`,
whose priority the backlog groomer inherits onto the ``fix_gripe`` todo it
mints). The ``PRIO:`` alias stays valid at the handler boundary — skills,
tests, and cached agent prompts keep writing the tag form — but it is
translated to the ``prio`` column and stripped from the stored tag set, so a
ref's priority lives in exactly one place (the column the doable-view sorts
on), never a redundant tag row that could drift from it.
"""

from __future__ import annotations

#: ``PRIO:`` tag → the canonical ``refs.prio`` column (1..10, lower = hotter),
#: the striving-weight scale the todo tree rotates on.
PRIO_TAG_TO_INT: dict[str, int] = {
    "PRIO:urgent": 1,
    "PRIO:high": 3,
    "PRIO:normal": 5,
    "PRIO:low": 8,
}


def split_prio(tags: list[str] | None) -> tuple[list[str] | None, int | None]:
    """Pull the last ``PRIO:`` tag out of ``tags`` and translate it to an int.

    Returns ``(tags_without_prio, prio_or_none)`` — the ``PRIO:`` alias is
    stripped so it never lands as a redundant closed-tag row alongside the
    column write. Unknown ``PRIO:`` values pass through untouched so the strict
    validator surfaces the typo with its options list.
    """
    if not tags:
        return tags, None
    out: list[str] = []
    found: int | None = None
    for t in tags:
        if t in PRIO_TAG_TO_INT:
            found = PRIO_TAG_TO_INT[t]
            continue
        out.append(t)
    return (out if out else None), found
