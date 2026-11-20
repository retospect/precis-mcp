"""A gripe's ``goal:<value>`` axis — deterministic thread/cluster mapping.

gr592749: no field recorded a gripe's *goal*, so "fixed gripes by goal"
counts had to be inferred from commit scopes. ``goal_of`` maps a gripe's
``thread:<slug>`` / ``cluster:<slug>`` open tags onto the closed goal
vocabulary; the triage tick stamps the result as ``goal:<value>`` and
``search(kind='gripe', view='count')`` facets on it via
``group_by='tag:goal'``.
"""

from collections.abc import Iterable
from fnmatch import fnmatchcase

#: The closed goal vocabulary (gr592749).
GOALS = (
    "reaction-graph",
    "pathway",
    "pcb",
    "memory-graph",
    "work-model",
    "search-surface",
    "llm-router",
    "other",
)

#: Ordered (glob, goal) rules matched against ``thread:<slug>`` and
#: ``cluster:<slug>`` tag values — the first hit wins, so thread rules
#: outrank the cluster catch-alls when both are present.
GOAL_MAP: tuple[tuple[str, str], ...] = (
    ("thread:catalysis-selectivity", "reaction-graph"),
    ("cluster:pathway-*", "pathway"),
    ("cluster:pcb-*", "pcb"),
    ("cluster:memory-*", "memory-graph"),
    ("cluster:garden", "memory-graph"),
    ("cluster:dark-factory", "work-model"),
    ("cluster:release", "work-model"),
    ("cluster:ship", "work-model"),
    ("cluster:work-model", "work-model"),
    ("cluster:result-size-cap", "search-surface"),
    ("cluster:agent-surface", "search-surface"),
    ("cluster:skill-*", "search-surface"),
    ("cluster:llm-*", "llm-router"),
    ("cluster:router", "llm-router"),
)


def goal_of(tags: Iterable[str]) -> str:
    """First ``GOAL_MAP`` hit over the gripe's ``thread:``/``cluster:`` tags.

    ``tags`` carries values the way the store spells them — bare
    ``thread:x``/``cluster:y`` for the OPEN/FLAG namespaces, ``NS:value``
    otherwise (the same normalisation ``classify_origin`` takes).
    No match → ``"other"``.
    """
    values = [t.removeprefix("OPEN:").removeprefix("FLAG:") for t in tags]
    scoped = [v for v in values if v.startswith(("thread:", "cluster:"))]
    for pattern, goal in GOAL_MAP:
        if any(fnmatchcase(v, pattern) for v in scoped):
            return goal
    return "other"
