"""Private gtd kind: capture one inbox item and read it back.

A private non-ref kind like learning: identity comes from the request
principal, never from tool arguments, and ``gtN`` handles are local to the
owner's store.
"""

from __future__ import annotations

import re
from typing import Any, ClassVar

from precis.dispatch import Hub
from precis.errors import BadInput, NotFound
from precis.gtd import GtdStore
from precis.protocol import Handler, KindSpec
from precis.response import Response

_NEXT = "put(kind='gtd', text='<the thing to capture>')"


class GtdHandler(Handler):
    """A private non-ref kind; identity is never taken from tool arguments."""

    spec: ClassVar[KindSpec] = KindSpec(
        kind="gtd",
        title="Personal gtd",
        description="Private inbox, actions and projects for the signed-in owner.",
        supports_get=True,
        supports_put=True,
        is_numeric=False,
        id_required=False,
        supports_chunk_selectors=False,
        placement="system",
        modes=(),
    )

    def __init__(self, *, hub: Hub) -> None:
        self._gtd = GtdStore(hub.live_store)

    def get(
        self,
        *,
        id: str | int | None = None,
        **_kw: Any,
    ) -> Response:
        if isinstance(id, str) and re.fullmatch(r"gt[1-9][0-9]*", id):
            item = self._gtd.get_item(int(id[2:]))
            captured = "(same as text)" if item.captured == item.text else item.captured
            return Response(
                body=f"gt{item.id} [{item.list}/{item.state}] {item.text}\n"
                f"captured: {captured}"
            )
        raise NotFound("gtd address not found", next="get(kind='gtd', id='gt<N>')")

    def put(
        self,
        *,
        id: str | int | None = None,
        text: str | None = None,
        **_kw: Any,
    ) -> Response:
        if id is not None:
            raise BadInput("gtd put only captures: pass text=", next=_NEXT)
        if text is None:
            raise BadInput("capture needs text=", next=_NEXT)
        item = self._gtd.capture(text)
        return Response(body=f"Captured: {item.captured}. gt{item.id}")
