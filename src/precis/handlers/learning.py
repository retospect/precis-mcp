"""Owner-only manual study activity, backed by dedicated learning tables.

These are self-reports of engagement, not successful recall or concept mastery.
No refs, chunks, embeddings, public graph handles or generated content are
created. The private runtime route avoids shared audit payloads/continuations.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict
from typing import Any, ClassVar

from precis.dispatch import Hub
from precis.errors import BadInput, NotFound
from precis.format import render_agent_table
from precis.learning import LearningStore
from precis.protocol import ArgsSpec, Handler, KindSpec
from precis.response import Response

_NEXT = "get(kind='learning', id='/activity')"


class LearningHandler(Handler):
    """A private non-ref kind; identity is never taken from tool arguments."""

    edit_id_list: ClassVar[str] = "refuse"
    ARGS: ClassVar[dict[str, ArgsSpec]] = {
        "get": ArgsSpec({"days"}),
        "put": ArgsSpec({"date", "status", "minutes", "note", "idempotency_key"}),
        "edit": ArgsSpec({"timezone", "typical_session_minutes"}),
    }
    spec: ClassVar[KindSpec] = KindSpec(
        kind="learning",
        title="Personal learning",
        description="Private self-reported study activity and personal pacing settings.",
        supports_get=True,
        supports_put=True,
        supports_edit=True,
        is_numeric=False,
        id_required=False,
        supports_chunk_selectors=False,
        placement="system",
        modes=(),
        edit_modes=("replace",),
    )

    def __init__(self, *, hub: Hub) -> None:
        self._learning = LearningStore(hub.live_store)

    def get(
        self,
        *,
        id: str | int | None = None,
        args: dict[str, Any] | None = None,
        **_kw: Any,
    ) -> Response:
        args = args or {}
        ident = "/activity" if id is None else str(id)
        if ident == "/activity":
            activity = self._learning.activity(days=args.get("days", 84))
            lines = [
                "# Your recorded study activity",
                f"timezone: {activity['timezone']}",
                f"today: {activity['today']}",
                f"last recorded study: {activity['last_studied'] or 'none'}",
                "source: self-reported; activity is not a mastery estimate",
            ]
            lines.append(
                render_agent_table(
                    [
                        {
                            "date": day["date"],
                            "status": day["status"],
                            "minutes": day["minutes"],
                        }
                        for day in activity["days"]
                    ],
                    schema=("date", "status", "minutes"),
                )
            )
            return Response(body="\n".join(lines))
        if args:
            raise BadInput("days= is only supported for /activity", next=_NEXT)
        if ident == "/settings":
            return self._json(asdict(self._learning.settings()))
        if re.fullmatch(r"le[1-9][0-9]*", ident):
            event = self._learning.get_event(int(ident[2:]))
            return self._json(asdict(event))
        raise NotFound("learning address not found", next=_NEXT)

    def put(
        self,
        *,
        id: str | int | None = None,
        args: dict[str, Any] | None = None,
        **_kw: Any,
    ) -> Response:
        if id not in (None, "/checkin"):
            raise BadInput("learning check-ins use id='/checkin'", next=_NEXT)
        args = args or {}
        key = args.get("idempotency_key")
        if not isinstance(key, str):
            raise BadInput(
                "a check-in requires a string idempotency_key",
                next="retry put(kind='learning', id='/checkin', args={"
                "'status': 'studied', 'idempotency_key': '<new unique key>'})",
            )
        event = self._learning.checkin(
            study_date=args.get("date"),
            status=args.get("status", "studied"),
            minutes=args.get("minutes"),
            note=args.get("note", ""),
            idempotency_key=key,
        )
        return Response(
            body=f"Recorded self-reported {event.status} for {event.study_date}: "
            f"le{event.id}. This records activity, not mastery."
        )

    def edit(
        self,
        *,
        id: str | int | None = None,
        mode: str = "replace",
        args: dict[str, Any] | None = None,
        **_kw: Any,
    ) -> Response:
        if id not in (None, "/settings") or mode != "replace":
            raise BadInput(
                "learning settings use id='/settings', mode='replace'",
                next="edit(kind='learning', id='/settings', mode='replace', "
                "args={'typical_session_minutes': 30, 'timezone': 'UTC'})",
            )
        args = args or {}
        if not args:
            raise BadInput(
                "provide timezone and/or typical_session_minutes",
                next="get(kind='learning', id='/settings')",
            )
        current = self._learning.settings()
        settings = self._learning.set_settings(
            timezone=args.get("timezone", current.timezone),
            typical_session_minutes=args.get(
                "typical_session_minutes", current.typical_session_minutes
            ),
        )
        return self._json(asdict(settings))

    @staticmethod
    def _json(value: dict[str, Any]) -> Response:
        return Response(body=json.dumps(value, ensure_ascii=False, default=str))
