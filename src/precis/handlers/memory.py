"""MemoryHandler — capture notes, decisions, ideas, questions.

Numeric-id ref kind. Refactored in phase 5 to subclass
:class:`NumericRefHandler` — the shared CRUD shape now lives in one
place across memory / todo / gripe / fc / conv.

Semantics from the `precis-memory-help` skill:
    - put(text=...)                — create new memory, return its id
    - tag(id=N, add=[...])         — add/replace tags on memory N
    - tag(id=N, remove=[...])      — remove tags from memory N
    - link(id=N, target='kind:id') — cross-link memory N to another ref
    - delete(id=N)                 — soft-delete memory N
    - get(id=N)                    — read memory text + tags
    - get(id='/recent')            — list recent memories
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
        supports_search_hits=True,
        supports_put=True,
        supports_delete=True,
        supports_tag=True,
        supports_link=True,
        is_numeric=True,
        id_required=False,
        note_like=True,
    )

    kind: ClassVar[str] = "memory"
    sense: ClassVar[str] = "memory"
