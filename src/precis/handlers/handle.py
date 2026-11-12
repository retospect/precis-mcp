"""Handle converter — numeric ref id <-> spoken word handle (gr648264).

``get(kind='handle', id=648264)`` -> ``"amber brave dog"`` and
``get(kind='handle', q='amber brave dog')`` -> ``648264``. Stateless,
computed in code (no LLM, no store, free); called only when needed, so normal
agent traffic never pays for word handles. The mapping lives in
:mod:`precis.handle_words`.
"""

from __future__ import annotations

from typing import Any, ClassVar

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handle_words import to_number, to_words
from precis.protocol import Handler, KindSpec
from precis.response import Response


class HandleHandler(Handler):
    #: converts ids; stores nothing: the agent-write secret gate (dispatch) skips this kind.
    stores_opaque_text: ClassVar[bool] = True
    spec: ClassVar[KindSpec] = KindSpec(
        kind="handle",
        title="Word handle",
        description=(
            "Converter between a numeric ref id and a short spoken word "
            "handle (three common words). id=648264 returns the words; "
            "q='amber brave dog' returns the number. Deterministic and "
            "reversible, free, no store. For voice use; the words are not "
            "accepted as id= by other kinds."
        ),
        supports_get=True,
        is_numeric=False,
        id_required=True,
        placement="system",
    )

    def __init__(self, *, hub: Hub) -> None:
        _ = hub

    def get(
        self,
        *,
        id: str | int | None = None,
        view: str | None = None,
        q: str | None = None,
        **_kw: Any,
    ) -> Response:
        src = id if id not in (None, "") else q
        if src in (None, ""):
            raise BadInput(
                "handle needs a number (id=) or words (q=)",
                next="get(kind='handle', id=648264)",
            )
        text = str(src).strip()
        if text.isdigit():
            words = to_words(int(text))
            return Response(body=f"{int(text)} = {words}\n")
        try:
            number = to_number(text)
        except ValueError as e:
            raise BadInput(
                f"not a word handle: {e}",
                next="get(kind='handle', q='amber brave dog')",
            ) from None
        return Response(body=f"{text.lower()} = {number}\n")
