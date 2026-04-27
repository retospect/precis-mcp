"""MemoryHandler — capture notes, decisions, ideas, questions.

Numeric-id ref kind. Refactored in phase 5 to subclass
:class:`NumericRefHandler` — the shared CRUD shape now lives in one
place across memory / todo / gripe / fc / conv / quest.

Semantics from the `precis-memory-help` skill:
    - put(text=...)            — create new memory, return its id
    - put(id=N, text=...)      — replace memory N's text
    - put(id=N, mode='delete') — soft-delete memory N
    - put(id=N, tags=[...])    — add/replace tags on memory N
    - get(id=N)                — read memory text + tags
    - get(id='/recent')        — list recent memories
    - search(q=...)            — lexical search over memories
"""

from __future__ import annotations

from typing import ClassVar

from precis.handlers._numeric_ref import NumericRefHandler
from precis.protocol import KindSpec


class MemoryHandler(NumericRefHandler):
    spec: ClassVar[KindSpec] = KindSpec(
        kind="memory",
        title="Memory",
        description=(
            "Notes, decisions, ideas, questions. Numeric id assigned on "
            "create. Sub-kind via 'kind:' open tag."
        ),
        supports_get=True,
        supports_search=True,
        supports_put=True,
        is_numeric=True,
        id_required=False,
    )

    kind: ClassVar[str] = "memory"
    sense: ClassVar[str] = "memory"
