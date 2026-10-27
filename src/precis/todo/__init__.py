"""``precis.todo`` — todo domain logic shared by handlers and workers.

Neutral home for what used to be ``todo/views`` and
``todo/guards`` so workers need not import handler-private modules
(import-linter contract in ``pyproject.toml``).
"""

#: STATUS values that close a todo — the one set every "is this todo
#: still open" SQL predicate shares (nursery detectors, doctor asks and
#: trackers, dispatch child gates, sweeper unpark, the todo views, the
#: quest unrun-searches lookup, memory-hub review todos). gr482126: each
#: site hardcoded its own copy of the literal while the tag verb let a
#: quest-vocabulary value (``STATUS:abandoned``) onto todos, where it
#: counted as *open* to every one of them. The write-side vocabulary
#: (``open`` / ``doing`` / ``blocked`` / ``paused`` + these terminals)
#: lives in ``store.types._KIND_STATUS_VALUES["todo"]`` — keep in sync.
TERMINAL_TODO_STATUSES: frozenset[str] = frozenset({"done", "won't-do", "auto-timeout"})

#: STATUS values that close a *blocked-by target* — the union every
#: "is the blocker still live" predicate must share. A blocked-by edge
#: can point at any kind, and terminal vocabularies differ per kind
#: (todo ``won't-do`` / ``auto-timeout``, gripe ``wontfix``, quest
#: ``abandoned`` — the terminals in ``store.types._KIND_STATUS_VALUES``)
#: — gr520237: the blocked-by checks hardcoded
#: ``('done', 'won''t-do')``, so a todo blocked-by a gripe closed
#: ``wontfix`` stayed blocked forever. ``resolved`` is deliberately
#: absent: the tag verb rewrites it to ``done`` at write time
#: (``handlers/_status_tag.STATUS_VALUE_ALIASES``), so it is never a
#: stored STATUS value. Self-side todo predicates keep
#: :data:`TERMINAL_TODO_STATUSES`; this set is for the *target* of a
#: blocked-by edge only.
BLOCKER_TERMINAL_STATUSES: frozenset[str] = frozenset(
    {"done", "won't-do", "auto-timeout", "wontfix", "abandoned"}
)


def blocker_terminal_sql() -> str:
    """The ``IN (…)`` literal-list fragment for
    :data:`BLOCKER_TERMINAL_STATUSES` — constant strings, no caller
    input, so interpolation is safe and matches the SQL-concat style
    of the views/workers that use it."""
    return (
        "("
        + ", ".join(
            "'" + v.replace("'", "''") + "'" for v in sorted(BLOCKER_TERMINAL_STATUSES)
        )
        + ")"
    )
