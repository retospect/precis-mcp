"""AgentLogHandler — read / triage side of ``kind='agentlog'``.

Numeric-id ref kind for run-attribution records: one per agentic pass
(plan_tick, operator change, chat follow-up) that touched the corpus,
carrying the assembled prompt + model/source + ``touched`` links to the
chunks it wrote. Subclasses :class:`NumericRefHandler` for the shared
CRUD shape.

Agentlogs are *produced* by run machinery through :mod:`precis.agentlog`
(``open_log`` / ``touch_from_env`` / ``finalize_log``), not by agents —
so this handler omits ``put`` / ``edit``. What it offers the agent
surface is the read / triage half:

    - get(kind='agentlog', id=N)         — read one run + its tags
    - get(kind='agentlog', id='/recent') — recent runs (newest-first)
    - search(kind='agentlog', q=...)     — lexical search over titles
    - tag(id=N, add/remove=[...])        — classify / annotate
    - link(id=N, target='kind:id')       — relate a run to a ref
    - delete(id=N)                       — soft-delete (history pruning)

Like alerts, agentlogs are NOT embedded (``emits_card`` stays False):
surfaced by the ``/agentlogs`` web tab, direct queries, and chunk
``connections`` (the ``touched`` edge), never by semantic search. See
:mod:`precis.agentlog` for the lifecycle.
"""

from __future__ import annotations

from typing import ClassVar

from precis.handlers._numeric_ref import NumericRefHandler
from precis.protocol import KindSpec


class AgentLogHandler(NumericRefHandler):
    spec: ClassVar[KindSpec] = KindSpec(
        kind="agentlog",
        title="Agent log",
        description=(
            "Run-attribution record — one per agentic run (plan_tick, "
            "operator change request, chat follow-up) that touched the "
            "corpus. Carries the full assembled prompt, model + source, "
            "and `touched` links to every chunk the run wrote/moved, so a "
            "suspicious chunk walks back to the run that produced it. "
            "Numeric id; GC'd past a retention window (links drop, chunks "
            "stay). Produced by run machinery, not hand-authored. Not "
            "embedded."
        ),
        supports_get=True,
        supports_search=True,
        supports_search_hits=True,
        # No put/edit: agentlogs are opened by run machinery via
        # precis.agentlog.open_log, never hand-authored through the
        # agent surface.
        supports_put=False,
        supports_edit=False,
        supports_delete=True,
        supports_tag=True,
        supports_link=True,
        is_numeric=True,
        id_required=False,
        note_like=True,
    )

    kind: ClassVar[str] = "agentlog"
    sense: ClassVar[str] = "agentlog"

    # ── list-view filters (id='/<view>') ────────────────────────────

    def _supported_list_views(self) -> tuple[str, ...]:
        return ("recent",)

    def _render_one(self, ref, tags):
        """Default render + the triage fields a run's meta carries.

        A run's outcome lives in ``meta`` (``status``/``error`` from
        ``finalize_log``, ``model``, the salvaged ``result`` text), and the
        base render hid all of it — a failed quest_tick's agentlog read as
        just a title, which is exactly the "no queryable surface" wall the
        doctor hit for weeks on gr244061 while the offending-output excerpt
        sat on the row. Surface the small fields inline and point at
        ``view='raw'`` for the rest.
        """
        body = super()._render_one(ref, tags)
        meta = ref.meta or {}
        lines: list[str] = []
        for key in ("status", "model", "error"):
            val = meta.get(key)
            if val:
                lines.append(f"{key}: {val}")
        result = meta.get("result")
        if isinstance(result, str) and result:
            excerpt = result[:400]
            more = f" … ({len(result)} chars total, view='raw' for all)"
            lines.append(f"result: {excerpt}{'' if len(result) <= 400 else more}")
        if lines:
            body += "\n\n" + "\n".join(lines)
        return body


__all__ = ["AgentLogHandler"]
