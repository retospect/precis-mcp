"""GripeHandler — informal log entries / complaints / scratch notes.

Numeric-id ref kind. Same shape as memory but with a different
intent: gripes are timestamped venting / observations the user wants
to capture without curating. Useful for retrospective review and as a
training corpus for personalisation.

No default tags. Search is lexical for now; semantic search lands
when block-level embeddings get wired into the state kinds.
"""

from __future__ import annotations

from typing import ClassVar

from precis.handlers._numeric_ref import NumericRefHandler
from precis.protocol import KindSpec


class GripeHandler(NumericRefHandler):
    spec: ClassVar[KindSpec] = KindSpec(
        kind="gripe",
        title="Gripe",
        description=(
            "Informal log entry — complaint, observation, scratch "
            "thought. Numeric id assigned on create. No structure beyond "
            "free-text body and optional tags."
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
    )

    kind: ClassVar[str] = "gripe"
    sense: ClassVar[str] = "gripe"
