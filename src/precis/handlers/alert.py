"""AlertHandler — read / triage side of ``kind='alert'``.

Numeric-id ref kind for machine-detected operational / health
conditions (worker spin loops, orphaned todos, stalled recurrings, …).
Subclasses :class:`NumericRefHandler` for the shared CRUD shape.

Alerts are *produced* by background passes through
:mod:`precis.alerts` (``raise_alert`` / ``resolve_stale_alerts``), not
by agents — so this handler intentionally omits ``put``. What it offers
the agent surface is the read / triage half:

    - get(kind='alert', id=N)        — read one alert + its tags
    - get(kind='alert', id='/recent')— recent alerts (open + resolved)
    - get(kind='alert', id='/open')  — currently-open alerts only
    - search(kind='alert', q=...)    — lexical search over alert titles
    - tag(id=N, add/remove=[...])    — ack / reclassify (resolve via
                                        add=['alert-state:resolved'],
                                        remove=['alert-state:open'])
    - link(id=N, target='kind:id')   — relate an alert to a ref
    - delete(id=N)                   — soft-delete (history pruning)

Unlike memory, alerts are NOT embedded (``emits_card`` stays False):
they're surfaced by the ``/alerts`` web tab and direct queries, never
by semantic search. See :mod:`precis.alerts` for the lifecycle.
"""

from __future__ import annotations

from typing import ClassVar

from precis.alerts import STATE_OPEN
from precis.handlers._numeric_ref import NumericRefHandler
from precis.protocol import KindSpec
from precis.response import Response


class AlertHandler(NumericRefHandler):
    spec: ClassVar[KindSpec] = KindSpec(
        kind="alert",
        title="Alert",
        description=(
            "Machine-detected operational / health condition — worker "
            "spin loops, orphaned todos, stalled recurrings, stale "
            "claims. Numeric id; deduped on meta.fingerprint; lifecycle "
            "via alert-state: open tags; source + severity via "
            "alert-source: / severity: tags. Produced by background "
            "passes, not hand-authored. Not embedded."
        ),
        supports_get=True,
        supports_search=True,
        supports_search_hits=True,
        # No put: alerts are raised by workers via precis.alerts.raise_alert,
        # never hand-authored through the agent surface.
        supports_put=False,
        supports_edit=False,
        supports_delete=True,
        supports_tag=True,
        supports_link=True,
        is_numeric=True,
        id_required=False,
        note_like=True,
    )

    kind: ClassVar[str] = "alert"
    sense: ClassVar[str] = "alert"

    # ── list-view filters (id='/<view>') ────────────────────────────

    def _supported_list_views(self) -> tuple[str, ...]:
        return ("recent", "open")

    def _list_view(self, view: str) -> Response | None:
        if view == "open":
            return self._render_open()
        return super()._list_view(view)

    def _render_open(self) -> Response:
        """Currently-open alerts, recency-ordered."""
        refs = self.store.list_refs(
            kind=self.kind, tags=[STATE_OPEN], limit=200
        )
        refs = sorted(refs, key=lambda r: r.updated_at, reverse=True)
        if not refs:
            return Response(body="no open alerts — all clear.")
        header = f"# {len(refs)} open alert{'' if len(refs) == 1 else 's'}"
        return Response(body=f"{header}\n{self._render_hits_table(refs)}")

    def _create_ack_next_hints(self, ref_id: int) -> list[tuple[str, str]]:
        # Alerts aren't put-created through this handler, but keep the
        # base hints coherent if a future producer path reuses the ack.
        return [
            (
                f"tag(kind='alert', id={ref_id}, "
                "add=['alert-state:resolved'], remove=['alert-state:open'])",
                "mark this alert resolved",
            ),
            *super()._create_ack_next_hints(ref_id),
        ]


__all__ = ["AlertHandler"]
