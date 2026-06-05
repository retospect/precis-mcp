"""GripeHandler — write-only friction capture.

Numeric-id ref kind. Same persistence shape as memory but with one
critical difference: **the agent surface is write-only**. The LLM
can ``put(kind='gripe', text='...')`` to drop a complaint, but
cannot read, list, search, tag, link, or delete what was filed.

The intent is a zero-ceremony "complaint box": the agent notices
friction (a misleading skill, a confusing error message, an
ergonomic gap) and files a half-sentence note in 5 seconds.
Triage happens out-of-band — human review via SQL or CLI tools —
so the agent never has to decide whether something is "important
enough" to file. If it's annoying, it goes in the box.

This is enforced via :class:`KindSpec` flags: only ``supports_put``
is True. Reading verbs raise ``Unsupported`` at the dispatch layer.

See ``precis-gripe-help`` for the agent-facing rationale.
"""

from __future__ import annotations

from typing import ClassVar

from precis.handlers._numeric_ref import NumericRefHandler
from precis.protocol import KindSpec, Response


class GripeHandler(NumericRefHandler):
    spec: ClassVar[KindSpec] = KindSpec(
        kind="gripe",
        title="Gripe",
        description=(
            "Write-only friction capture. The agent files complaints "
            "via put(kind='gripe', text=...); reads happen out-of-band "
            "(human triage via SQL / CLI). No agent-facing get, "
            "search, tag, link, or delete."
        ),
        supports_put=True,
        # Deliberately False: gripe is write-only from the agent surface.
        # The reading / triage path is human-only, off the MCP boundary.
        supports_get=False,
        supports_search=False,
        supports_search_hits=False,
        supports_delete=False,
        supports_tag=False,
        supports_link=False,
        is_numeric=True,
        id_required=False,
        note_like=True,
    )

    kind: ClassVar[str] = "gripe"
    sense: ClassVar[str] = "gripe"

    def _render_create_ack(self, ref_id: int) -> Response:
        # Broad usability pass 2026-05-30 (#5): the previous bare
        # ``created gripe id=N`` ack gave no signal that the write was
        # final. Agents told elsewhere to "clean up what you create"
        # would then call ``delete(kind='gripe', ...)``, get an
        # ``Unsupported`` error, and have no way to retract. Naming the
        # irreversibility on the success response closes that loop
        # without touching the write-only invariant.
        return Response(
            body=(
                f"created gripe id={ref_id} (write-only — gripes cannot "
                "be read, edited, or deleted via the MCP surface; triage "
                "happens out-of-band)"
            )
        )
