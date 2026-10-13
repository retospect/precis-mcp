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
