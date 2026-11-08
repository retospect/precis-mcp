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

One axis, two spellings (gr592664): the column stores the 1..10 number, and
``PRIO:`` accepts it both as a name (``PRIO:high`` → 3) and as the number
itself (``PRIO:3`` → 3).
"""

from __future__ import annotations

import re

#: ``PRIO:`` tag → the canonical ``refs.prio`` column (1..10, lower = hotter),
#: the striving-weight scale the todo tree rotates on.
PRIO_TAG_TO_INT: dict[str, int] = {
    "PRIO:urgent": 1,
    "PRIO:high": 3,
    "PRIO:normal": 5,
    "PRIO:low": 8,
}


#: Near-miss ``PRIO:`` spellings with one reading on the tag scale
#: (gr494831 step 2; prod 2026-10-15 put(kind='gripe', tags=['PRIO:medium'])).
#: ``PRIO:p2`` is deliberately absent: a p-prefix reads as the P0..P4 scale,
#: not the column, so it keeps refusing (gr592664 lifted only the bare
#: number — ``PRIO:2`` is now the column spelling, no alias needed).
PRIO_TAG_ALIASES: dict[str, str] = {"PRIO:medium": "PRIO:normal"}


#: The numeric ``PRIO:`` spelling — bare ASCII digits only (``PRIO:3``,
#: ``PRIO:10``); the p-prefixed P0..P4 shape is not the column scale.
_PRIO_NUMERIC_RE = re.compile(r"PRIO:([0-9]+)")


def prio_tag_int(tag: object) -> int | None:
    """The ``refs.prio`` value a recognised ``PRIO:`` spelling writes.

    Both spellings of the one 1..10 axis (gr592664): the named ladder
    (``PRIO:high`` → 3) and the column number itself (``PRIO:3`` → 3) —
    the names are aliases onto the number the column stores. Anything
    else — ``PRIO:bogus``, ``PRIO:0``, ``PRIO:11``, ``PRIO:p3``, non-str —
    returns ``None`` so ``Tag.parse_strict`` surfaces it as a typo.
    """
    if not isinstance(tag, str):
        return None
    if (named := PRIO_TAG_TO_INT.get(tag)) is not None:
        return named
    m = _PRIO_NUMERIC_RE.fullmatch(tag)
    if m is None:
        return None
    n = int(m.group(1))
    return n if 1 <= n <= 10 else None


def alias_prio_tags(tags: list[str] | None) -> tuple[list[str] | None, list[str]]:
    """Rewrite :data:`PRIO_TAG_ALIASES` spellings in ``tags`` to canonical.

    Returns ``(tags, readings)``; each reading is one
    ``'PRIO:medium' read as 'PRIO:normal'`` phrase for the reply's
    ``interpreted:`` line (:mod:`precis.handlers._interpret`).
    """
    if not tags:
        return tags, []
    out: list[str] = []
    readings: list[str] = []
    for t in tags:
        canon = PRIO_TAG_ALIASES.get(t.strip()) if isinstance(t, str) else None
        if canon is None:
            out.append(t)
            continue
        readings.append(f"{t.strip()!r} read as {canon!r}")
        out.append(canon)
    return out, readings


def validate_prio(prio: int | None) -> int | None:
    """Range-check a ``prio=`` kwarg (1..10) at the handler boundary.

    Returns ``prio`` on success (None passes through). Raises
    :class:`~precis.errors.BadInput` naming the accepted range and the
    conventional anchor values, so the agent-facing error teaches the
    scale rather than just rejecting.
    """
    from precis.errors import BadInput

    if prio is None:
        return None
    if not isinstance(prio, int) or isinstance(prio, bool):
        raise BadInput(
            f"prio must be an int 1..10, got {type(prio).__name__} {prio!r}",
            next="prio=1 (chat / preempt), prio=2 (cron), prio=5 (default)",
        )
    if prio < 1 or prio > 10:
        raise BadInput(
            f"prio out of range: {prio} (must be 1..10)",
            next="prio=1 preempts strategic rotation; 3..10 ride the 1/N share",
        )
    return prio


def split_prio(tags: list[str] | None) -> tuple[list[str] | None, int | None]:
    """Pull the last ``PRIO:`` tag out of ``tags`` and translate it to an int.

    Returns ``(tags_without_prio, prio_or_none)`` — the ``PRIO:`` alias is
    stripped so it never lands as a redundant closed-tag row alongside the
    column write. Both spellings translate (:func:`prio_tag_int`); unknown
    ``PRIO:`` values pass through untouched so the strict validator surfaces
    the typo with its options list.
    """
    if not tags:
        return tags, None
    out: list[str] = []
    found: int | None = None
    for t in tags:
        if (n := prio_tag_int(t)) is not None:
            found = n
            continue
        out.append(t)
    return (out if out else None), found
