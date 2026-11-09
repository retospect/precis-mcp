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


#: Inverse of :data:`PRIO_TAG_TO_INT` — the ladder name a stored column
#: value renders as (the name spelling of the same number). Off-ladder
#: values (2, 4, 6, 7, 9, 10) have no name and render bare.
PRIO_INT_TO_NAME: dict[int, str] = {
    v: k.removeprefix("PRIO:") for k, v in PRIO_TAG_TO_INT.items()
}


#: Near-miss ``PRIO:`` spellings with one reading on the tag scale
#: (gr494831 step 2; prod 2026-10-15 put(kind='gripe', tags=['PRIO:medium'])).
#: ``PRIO:p2`` is deliberately absent: a p-prefix reads as the P0..P4 scale,
#: not the column, so it keeps refusing (gr592664 lifted only the bare
#: number — ``PRIO:2`` is now the column spelling, no alias needed).
PRIO_TAG_ALIASES: dict[str, str] = {"PRIO:medium": "PRIO:normal"}

#: The rungs a gripe's ``prio`` accepts (gr604593): the named ladder,
#: nothing between. Post-split that column is a human judgement only —
#: the off-rung numbers were machine band scores, which now live on
#: ``meta.dispatch_score``/``meta.dispatch.severity``
#: (:mod:`precis.utils.dispatch_score`). Other kinds keep the full
#: 1..10: todo's ``prio=2 (cron)`` and job prios are not this axis.
GRIPE_PRIO_RUNGS: frozenset[int] = frozenset(PRIO_TAG_TO_INT.values())


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


def validate_prio(
    prio: int | None, *, rungs: frozenset[int] | None = None
) -> int | None:
    """Range-check a ``prio=`` kwarg (1..10) at the handler boundary.

    Returns ``prio`` on success (None passes through). Raises
    :class:`~precis.errors.BadInput` naming the accepted range and the
    conventional anchor values, so the agent-facing error teaches the
    scale rather than just rejecting. ``rungs`` narrows the accepted
    set to a named ladder — the gripe handler passes
    :data:`GRIPE_PRIO_RUNGS`: its column is a human judgement, so an
    off-rung number (a machine band score under the old shared column)
    refuses with the real spellings.
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
    if rungs is not None and prio not in rungs:
        names = ", ".join(f"{n} ({PRIO_INT_TO_NAME[n]})" for n in sorted(rungs))
        raise BadInput(
            f"prio {prio} is off the named rungs — the judgement ladder is {names}",
            options=[f"prio={n}" for n in sorted(rungs)],
            next=(
                "off-rung numbers were machine band scores; sequencing now "
                "lives on meta.dispatch_score, the prio column is human-only"
            ),
        )
    return prio


def prio_display(prio: int) -> str:
    """The display spelling of a stored ``refs.prio`` value.

    One axis, two spellings — a read renders both: the stored number
    plus its ladder name when it has one (``3 (high)``); off-ladder
    values (``2``) have no name and render as the bare number. Storage
    stays numeric; only the render changes.
    """
    name = PRIO_INT_TO_NAME.get(prio)
    return f"{prio} ({name})" if name is not None else str(prio)


def split_prio(
    tags: list[str] | None, *, rungs: frozenset[int] | None = None
) -> tuple[list[str] | None, int | None]:
    """Pull the last ``PRIO:`` tag out of ``tags`` and translate it to an int.

    Returns ``(tags_without_prio, prio_or_none)`` — the ``PRIO:`` alias is
    stripped so it never lands as a redundant closed-tag row alongside the
    column write. Both spellings translate (:func:`prio_tag_int`); unknown
    ``PRIO:`` values pass through untouched so the strict validator surfaces
    the typo with its options list. ``rungs`` (the gripe handler's
    :data:`GRIPE_PRIO_RUNGS`) refuses a recognised but off-rung
    ``PRIO:<n>`` outright — passing it on would store it as a raw closed
    tag, not the column value it spells.
    """
    from precis.errors import BadInput

    if not tags:
        return tags, None
    out: list[str] = []
    found: int | None = None
    for t in tags:
        if (n := prio_tag_int(t)) is not None:
            if rungs is not None and n not in rungs:
                raise BadInput(
                    f"{t.strip()!r} is off the named rungs — the judgement "
                    "ladder takes "
                    + ", ".join(f"{v} ({PRIO_INT_TO_NAME[v]})" for v in sorted(rungs)),
                    options=sorted(PRIO_TAG_TO_INT),
                    next=(
                        "off-rung numbers were machine band scores; "
                        "sequencing now lives on meta.dispatch_score"
                    ),
                )
            found = n
            continue
        out.append(t)
    return (out if out else None), found
