"""``STATUS:`` near-miss values with exactly one reading → canonical.

gr508686 (7d ``tool_calls`` ledger): ``tag(kind='gripe',
add=['STATUS:closed'])`` was the largest caller-error family —
``closed``/``resolved``/``fixed``/``landed`` are the natural words for
``done``. An alias applies only when its canonical target is in the
kind's own STATUS vocabulary, and every rewrite is announced on the
reply's ``interpreted:`` line — the same forgiving-inputs contract as
:mod:`precis.handlers._prio_tag` (``PRIO:medium`` → ``PRIO:normal``).
A guessable value with two readings (``wontfix`` candidates like
``wont``/``abandoned`` vary per kind) stays refused with ``options=``.
"""

from __future__ import annotations

from precis.store.types import _KIND_STATUS_VALUES

#: Near-miss ``STATUS:`` value (lowercased) → canonical. Applied only
#: when the canonical target is a real value on the calling kind.
STATUS_VALUE_ALIASES: dict[str, str] = {
    "closed": "done",
    "resolved": "done",
    "fixed": "done",
    "landed": "done",
}


def alias_status_tags(
    tags: list[str] | None, *, kind: str
) -> tuple[list[str] | None, list[str]]:
    """Rewrite :data:`STATUS_VALUE_ALIASES` spellings in ``tags`` to canonical.

    Returns ``(tags, readings)``; each reading is one
    ``'STATUS:closed' read as 'STATUS:done'`` phrase for the reply's
    ``interpreted:`` line (:mod:`precis.handlers._interpret`).
    """
    valid = _KIND_STATUS_VALUES.get(kind)
    if not tags or not valid:
        return tags, []
    out: list[str] = []
    readings: list[str] = []
    for t in tags:
        canon = None
        if isinstance(t, str) and t.startswith("STATUS:"):
            target = STATUS_VALUE_ALIASES.get(t[len("STATUS:") :].strip().lower())
            if target is not None and target in valid:
                canon = f"STATUS:{target}"
        if canon is None:
            out.append(t)
            continue
        readings.append(f"{t.strip()!r} read as {canon!r}")
        out.append(canon)
    return out, readings
