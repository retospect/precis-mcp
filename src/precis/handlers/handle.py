"""Handle converter — numeric ref id <-> spoken word handle (gr648264).

``get(kind='handle', id=648264)`` -> ``"amber brave dog"`` and
``get(kind='handle', q='amber brave dog')`` -> ``648264``. Word conversion
is computed without a store. Descriptive queries with an explicit reference
kind use hybrid search, returning candidates rather than guessing an identity:
derived spoken names may collide and are deliberately not stored aliases.
The reversible mapping lives in :mod:`precis.handle_words`.
"""

from __future__ import annotations

from typing import Any, ClassVar

from precis.dispatch import Hub
from precis.errors import BadInput, NotFound
from precis.format import serialize
from precis.handle_words import to_number, to_words
from precis.protocol import ArgsSpec, Handler, KindSpec
from precis.response import Response
from precis.spoken_names import spoken_name
from precis.utils import handle_registry
from precis.utils.ref_hybrid import fused_ref_hits


class HandleHandler(Handler):
    ARGS: ClassVar[dict[str, ArgsSpec]] = {"get": ArgsSpec({"kind": str})}
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
            "accepted as id= by other kinds. A descriptive q plus "
            "args={'kind':'paper'} returns up to five spoken-name candidates."
        ),
        supports_get=True,
        is_numeric=False,
        id_required=True,
        placement="system",
    )

    def __init__(self, *, hub: Hub) -> None:
        self.hub = hub

    def get(
        self,
        *,
        id: str | int | None = None,
        view: str | None = None,
        q: str | None = None,
        args: dict[str, Any] | None = None,
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
            if id in (None, "") and args and args.get("kind"):
                return self._resolve(text, args["kind"])
            raise BadInput(
                f"not a word handle: {e}",
                next="get(kind='handle', q='amber brave dog')",
            ) from None
        return Response(body=f"{text.lower()} = {number}\n")

    def _resolve(self, text: str, kind: str) -> Response:
        if kind not in {"paper", "gripe", "todo", "memory"}:
            raise BadInput(
                "spoken-name resolution needs a reference kind",
                options=["paper", "gripe", "todo", "memory"],
                next="get(kind='handle', q='the Miller paper', args={'kind':'paper'})",
            )
        # Nested reads must obey the same credential scope as a direct search.
        from precis.runtime.dispatch import _KIND_GATE

        gate = _KIND_GATE.get()
        if gate is not None:
            gate("search", {"kind": kind, "q": text})
        if self.hub.store is None:
            raise NotFound(
                "spoken-name search needs the reference store",
                next=f"search(kind={kind!r}, q={text!r})",
            )
        refs = fused_ref_hits(
            self.hub.store, self.hub.embedder, q=text, kind=kind, limit=5
        )
        if not refs:
            raise NotFound(
                f"no spoken-name candidates for {text!r}",
                next=f"search(kind={kind!r}, q={text!r})",
            )
        rows = []
        for ref in refs:
            meta = dict(ref.meta or {})
            if ref.authors:
                meta["authors"] = ref.authors
            if ref.year:
                meta["year"] = ref.year
            handle = (
                handle_registry.try_format(ref.kind, ref.id) or f"{ref.kind}:{ref.id}"
            )
            summary = " ".join(ref.title.split())
            rows.append(
                {
                    "handle": handle,
                    "spoken name": spoken_name(ref.kind, summary, meta),
                    "summary": summary,
                }
            )
        # The lookup returns one line per candidate, without a table heading.
        _, _, candidates = serialize(rows, format="toon", sep=" | ").partition("\n")
        return Response(body=candidates + "\n")
