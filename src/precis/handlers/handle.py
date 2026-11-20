"""Handle converter — numeric ref id <-> spoken word handle (gr648264, gr674040).

``get(kind='handle', id=648264)`` -> ``"brave dog"`` — the two-word pair,
adjective + noun for ``id % 16384`` (:mod:`precis.handle_words`). When
another live ref shares the pair the answer is the full three-word form
plus a clash note, because two words can't pick between them by ear.
``id=[...]`` answers one ``id = words`` line per element.
``get(kind='handle', q='amber brave dog')`` -> ``648264``;
``q='brave dog'`` lists the live refs sharing that pair (``of_kind=``
narrows by kind; ``args={'page': N}`` pages a long candidate list).
``put(kind='handle', text='on'|'off')`` pins the
session's word-handle switch (:mod:`precis.word_handle_session`) — while
on, search and list rows gain a ``word`` column so a voice agent can
speak each row without a lookup per id.

Word maps are computed in code (no LLM, free); only the pair-collision
probe touches the store, and a stateless runtime simply can't see
clashes — the pair is still the best spoken form there.

A descriptive ``q`` plus ``args={'kind': 'paper'|'gripe'|'todo'|'memory'}``
(not a word handle) runs hybrid search and returns up to five
spoken-name candidates rather than guessing an identity: derived spoken
names may collide and are deliberately not stored aliases.
"""

from __future__ import annotations

from typing import Any, ClassVar

from precis import word_handle_session
from precis.dispatch import Hub
from precis.errors import BadInput, Denied, NotFound
from precis.format import render_agent_table, serialize
from precis.handle_words import pair_residue, to_number, to_words, to_words2
from precis.protocol import ArgsSpec, Handler, KindSpec
from precis.response import Response
from precis.spoken_names import spoken_name
from precis.utils import handle_registry
from precis.utils.next_block import render_next_section
from precis.utils.ref_hybrid import fused_ref_hits

#: Residue space of a two-word pair: adjective x noun lists, base 128 each.
_PAIR_MODULUS = 128 * 128

#: ``q='<pair>'`` candidate list paging (args={'page': N, 'page_size': M}).
_PAGE_SIZE_DEFAULT = 20
_PAGE_SIZE_MAX = 100


def _read_scope(hub: Hub) -> dict[str, Any]:
    """The caller's read scope for the pair probes (gr674040 review): the
    connection domain's kind and ``SPACE:`` fences plus the acting
    identity for owner-scoped profiles, so a pair never names or counts a
    ref the caller could not read."""
    from precis import profiles
    from precis.runtime.dispatch import _KIND_GATE
    from precis.runtime.scope import current_domain

    domain = current_domain()
    kinds = set(domain.kinds) if domain.kinds is not None else None
    gate = _KIND_GATE.get()
    if gate is not None:
        allowed = set()
        for kind in hub.kinds if kinds is None else kinds:
            try:
                gate("search", {"kind": kind})
            except Denied:
                continue
            allowed.add(kind)
        kinds = allowed
    return {
        "kinds": sorted(kinds) if kinds is not None else None,
        "spaces": sorted(domain.spaces) if domain.spaces is not None else None,
        "viewer": profiles.ACTING,
    }


def _page_args(page: Any, page_size: Any) -> tuple[int, int]:
    """``(page, page_size)`` validated, youtube-listing style."""
    try:
        p = max(1, int(page or 1))
    except (TypeError, ValueError):
        raise BadInput(
            f"page must be a positive integer, got {page!r}",
            next="get(kind='handle', q='<adjective> <noun>')",
        ) from None
    try:
        size = int(page_size) if page_size is not None else _PAGE_SIZE_DEFAULT
    except (TypeError, ValueError):
        raise BadInput(
            f"page_size must be a positive integer ≤ {_PAGE_SIZE_MAX}, got {page_size!r}",
            next="get(kind='handle', q='<adjective> <noun>', args={'page_size': 20})",
        ) from None
    if not 1 <= size <= _PAGE_SIZE_MAX:
        raise BadInput(
            f"page_size must be a positive integer ≤ {_PAGE_SIZE_MAX}, got {page_size!r}",
            next="get(kind='handle', q='<adjective> <noun>', args={'page_size': 20})",
        )
    return p, size


class HandleHandler(Handler):
    #: ``kind`` scopes the spoken-name search; ``of_kind``/``page``/
    #: ``page_size`` narrow and page a two-word candidate list (dispatch
    #: leaves these values nested in args).
    ARGS: ClassVar[dict[str, ArgsSpec]] = {
        "get": ArgsSpec({"kind": str, "of_kind": str, "page": int, "page_size": int})
    }
    #: converts ids; stores nothing: the agent-write secret gate (dispatch) skips this kind.
    stores_opaque_text: ClassVar[bool] = True
    spec: ClassVar[KindSpec] = KindSpec(
        kind="handle",
        title="Word handle",
        description=(
            "Converter between a numeric ref id and a spoken word handle. "
            "id=648264 returns two words ('brave dog' — the id mod 16384 "
            "pair); another live ref sharing the pair returns the "
            "three-word form with a clash note instead. id=[...] returns "
            "one 'id = words' line per id. q='amber brave dog' returns "
            "the number; q='brave dog' returns the live refs sharing the "
            "pair (of_kind= narrows, args={'page': N} pages). "
            "put(text='on'|'off') pins this "
            "session's word-handle switch — while on, search and list "
            "rows gain a 'word' column. The words are not accepted as "
            "id= by other kinds. A descriptive q plus "
            "args={'kind':'paper'} returns up to five spoken-name candidates."
        ),
        supports_get=True,
        supports_put=True,
        is_numeric=False,
        id_required=True,
        placement="system",
    )

    def __init__(self, *, hub: Hub) -> None:
        self.hub = hub

    def get(
        self,
        *,
        id: str | int | list[str | int] | None = None,
        view: str | None = None,
        q: str | None = None,
        of_kind: str | None = None,
        page: int | None = None,
        page_size: int | None = None,
        args: dict[str, Any] | None = None,
        **_kw: Any,
    ) -> Response:
        args = args or {}
        of_kind = of_kind if of_kind is not None else args.get("of_kind")
        page = page if page is not None else args.get("page")
        page_size = page_size if page_size is not None else args.get("page_size")
        if isinstance(id, (list, tuple)):
            if page is not None or page_size is not None:
                raise BadInput(
                    "page=/page_size= page a two-word candidate list, "
                    "not an id= lookup",
                    next="get(kind='handle', q='<adjective> <noun>', args={'page': 2})",
                )
            if not id:
                raise BadInput(
                    "handle id list is empty",
                    next="get(kind='handle', id=648264)",
                )
            return Response(body="".join(self._id_line(x, one_line=True) for x in id))
        src = id if id not in (None, "") else q
        if src in (None, ""):
            raise BadInput(
                "handle needs a number (id=) or words (q=)",
                next="get(kind='handle', id=648264)",
            )
        text = str(src).strip()
        if text.isdigit():
            if page is not None or page_size is not None:
                raise BadInput(
                    "page=/page_size= page a two-word candidate list, "
                    "not an id= lookup",
                    next="get(kind='handle', q='<adjective> <noun>', args={'page': 2})",
                )
            return Response(body=self._id_line(text))
        spoken_kind = (args or {}).get("kind") if id in (None, "") else None
        return self._words_response(
            text,
            of_kind=of_kind,
            page=page,
            page_size=page_size,
            spoken_kind=spoken_kind,
        )

    def put(self, *, text: str | None = None, **_kw: Any) -> Response:
        """Flip this session's word-handle switch: ``text='on'`` or ``'off'``.

        Nothing is created — the verb is the user-facing "use word
        handles" affordance; the flag lives on the bound MCP session
        (:mod:`precis.word_handle_session`). A sessionless ``put`` has
        nothing to pin the flag to, so the ack says the switch did not
        take rather than pretend.
        """
        value = (text or "").strip().lower()
        if value not in ("on", "off"):
            raise BadInput(
                "handle put flips the session word-handle switch: text='on' or 'off'",
                next="put(kind='handle', text='on')",
            )
        on = value == "on"
        if not word_handle_session.set_enabled(on):
            return Response(
                body=(
                    "word handles stay off: the switch is per-session and "
                    "no session is bound — nothing to pin it on\n"
                )
            )
        body = f"word handles {value} for this session.\n"
        if on:
            body += (
                "search and list rows gain a 'word' column; "
                "say the words, never the number.\n"
            )
        return Response(body=body)

    # ── internals ────────────────────────────────────────────────────

    def _id_line(self, x: Any, *, one_line: bool = False) -> str:
        """One ``id = words`` line — two words, or three plus a clash note.

        ``one_line`` (batch calls) keeps the note on the same physical
        line, so a list answers exactly one line per input id.
        """
        text = str(x).strip()
        if not text.isdigit():
            return self._number_line(text)
        n = int(text)
        sep = "  # " if one_line else "\n"
        pair = to_words2(n)
        residue = pair_residue(pair)
        total = self._candidate_count(residue, exclude_id=n)
        if not total:
            return f"{n} = {pair}\n"
        others = self._candidates(residue, exclude_id=n, limit=4) or []
        if not others:
            # The sharers retired between the count and the name fetch.
            return (
                f"{n} = {to_words(n)}"
                f"{sep}clash: '{pair}' names {total} other live ref(s) — "
                "say all three words\n"
            )
        names = ", ".join(
            handle_registry.try_format(c.kind, c.id) or f"{c.kind}:{c.id}"
            for c in others
        )
        more = f" +{total - len(others)} more" if total > len(others) else ""
        return (
            f"{n} = {to_words(n)}"
            f"{sep}clash: '{pair}' also names {names}{more} — say all three words\n"
        )

    def _number_line(self, text: str) -> str:
        """Words -> number: the 3+-word full form via :func:`to_number`."""
        try:
            number = to_number(text)
        except ValueError as e:
            raise BadInput(
                f"not a word handle: {e}",
                next="get(kind='handle', q='amber brave dog')",
            ) from None
        return f"{text.lower()} = {number}\n"

    def _words_response(
        self,
        text: str,
        *,
        of_kind: str | None,
        page: Any = None,
        page_size: Any = None,
        spoken_kind: str | None = None,
    ) -> Response:
        """``q=`` words: a two-word pair resolves to its live candidates."""
        try:
            residue = pair_residue(text)
        except ValueError:
            if page is not None or page_size is not None:
                raise BadInput(
                    "page=/page_size= page a two-word candidate list, "
                    "not a full word-handle read",
                    next="get(kind='handle', q='<adjective> <noun>', args={'page': 2})",
                ) from None
            # Not a pair — the full-word parser owns it (3+ words, or
            # its BadInput on malformed input).
            if spoken_kind:
                try:
                    to_number(text)
                except ValueError:
                    # Not a word handle: a spoken description to search.
                    return self._resolve(text, spoken_kind)
            return Response(body=self._number_line(text))
        p, size = _page_args(page, page_size)
        pair = to_words2(residue)
        cands = self._candidates(
            residue, kind=of_kind, limit=size, offset=(p - 1) * size
        )
        if cands is None:
            raise BadInput(
                "a two-word handle needs a store to resolve which ref it names",
                next=f"get(kind='handle', q='{to_words(residue)}')",
            )
        total = self._candidate_count(residue, kind=of_kind) or 0
        if not cands:
            if p > 1 and total:
                pages = -(-total // size)
                return Response(
                    body=(
                        f"'{pair}' names {total} live ref(s) in {pages} "
                        f"page(s) — page {p} is past the end\n"
                    ),
                    result_count=0,
                )
            return Response(body=f"'{pair}' names no live ref\n", result_count=0)
        rows = [
            {
                "handle": handle_registry.try_format(c.kind, c.id)
                or f"{c.kind}:{c.id}",
                "words": to_words(c.id),
                "kind": c.kind,
                "summary": (c.title or "").splitlines()[0] if c.title else "",
            }
            for c in cands
        ]
        pages = -(-total // size)
        header = f"'{pair}' names {total} live ref(s)"
        if pages > 1:
            header += f", page {p} of {pages}"
        body = (
            header
            + " — say the three-word form to pick one:\n"
            + render_agent_table(rows, schema=["handle", "words", "kind", "summary"])
        )
        if p < pages:
            kind_arg = f", 'of_kind': {of_kind!r}" if of_kind else ""
            body += render_next_section(
                [
                    (
                        f"get(kind='handle', q={pair!r}, "
                        f"args={{'page': {p + 1}, 'page_size': {size}{kind_arg}}})",
                        f"page {p + 1} of {pages}",
                    )
                ]
            )
        return Response(
            body=body,
            result_count=len(cands),
            result_ids=[c.id for c in cands],
        )

    def _candidates(
        self,
        residue: int,
        *,
        kind: str | None = None,
        exclude_id: int | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[Any] | None:
        """Live refs sharing ``residue`` — ``None`` when no store is bound."""
        store = getattr(self.hub, "store", None)
        if store is None:
            return None
        return store.refs_sharing_id_residue(
            residue,
            modulus=_PAIR_MODULUS,
            kind=kind,
            exclude_id=exclude_id,
            limit=limit,
            offset=offset,
            **_read_scope(self.hub),
        )

    def _candidate_count(
        self,
        residue: int,
        *,
        kind: str | None = None,
        exclude_id: int | None = None,
    ) -> int | None:
        """Total live sharers under the same filters — ``None`` storeless."""
        store = getattr(self.hub, "store", None)
        if store is None:
            return None
        return store.count_refs_sharing_id_residue(
            residue,
            modulus=_PAIR_MODULUS,
            kind=kind,
            exclude_id=exclude_id,
            **_read_scope(self.hub),
        )

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
