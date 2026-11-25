"""``DiagramHandler`` — the MCP surface (get/put/edit/delete/link) shared by
every diagram kind (slice 4 factoring).

A diagram kind is a slug-addressed ref on the ``draft`` chunk-tree substrate
(the :class:`~precis.store._draft_ops.DraftStore` ops, reached as
``store.drafts``, parameterised by the kind), never exported
(``corpus_role='none'``). The CRUD is identical across
languages — create the ref + source chunk (+ optional vocab), edit the source /
vocab / notes (/ bounds), render a bounded lint preview + source + vocab +
notes + node→chunk bindings (in that order, so the byte cap can only cut the
auxiliary prose — gr793934; the full lint list pages under ``view='lints'``,
so many dangling bindings still can't push the source out), bind a source
element to the chunk it depicts — differing only in
the per-kind config carried on :class:`~precis.diagram.lang.DiagramLang` (the
handle scheme, the source language mechanics, the display nouns, and whether
the kind has a bounds/viewBox axis).

Two handle conventions ride on the same substrate: the ref's record handle
(``mm<N>`` / ``fg<N>``) and every chunk's node handle (``mn<N>`` /
``fn<N>``). The node handle IS the address — ``get(id='mn<N>')`` reads that
one chunk — so both kinds declare ``supports_chunk_selectors=False``: the
dispatcher must not rewrite a resolved node handle to ``slug~ord`` (a
selector this kind has no grammar for — gr793934, same class as plan's
gr495745). A node read pages by ``args={'offset': N}`` (character offsets,
memory's ``~body`` shape) when the chunk alone exceeds the cap, so the
continuation chain still reassembles it whole; a node takes no ``view=`` —
the inspection views (``links`` / ``log`` / ``raw`` / ``lints``) belong to
the owning ref.

Concrete handlers are thin: they set ``LANG`` + a ``spec`` and inherit
everything here. ``figure`` (SVG, with a viewBox) and ``mermaid`` (auto-layout,
no bounds) are the two instances.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, ClassVar

from precis.diagram.lang import DiagramLang
from precis.errors import BadInput, NotFound, ResultBudgetExceeded
from precis.handlers._slug_ref_shared import render_slug_ref_list
from precis.handlers._slug_ref_shared import resolve_live_slug_ref as _resolve_live
from precis.protocol import ArgsSpec, Handler
from precis.response import Response
from precis.utils.text import fmt_num

#: Lint lines the slug render shows before naming the paged
#: ``view='lints'`` read — an unbounded list ahead of the source could
#: still push it out (gr793934's second bite, review td794121).
_LINT_PREVIEW = 20


class DiagramHandler(Handler):
    #: Set by each concrete subclass — the source language + per-kind config.
    LANG: ClassVar[DiagramLang]

    #: ``edit(id=[...])`` repeats the same edit per id in one transaction
    #: (gr514482) — every write goes through ``self.store``.
    edit_id_list: ClassVar[str] = "repeat"

    #: args= keys ``get`` reads out of ``args`` (gr475332): ``links``'s
    #: ``rel`` / ``of_kind`` / ``limit`` / ``cursor``, ``log``'s
    #: ``limit`` / ``source``, ``raw``'s ``meta_key``, and the ``offset``
    #: page continuation on ``lints`` and a node-handle read (gr793934).
    ARGS: ClassVar[dict[str, ArgsSpec]] = {
        "get": ArgsSpec(
            {"rel", "of_kind", "limit", "cursor", "meta_key", "offset", "source"}
        )
    }

    #: args= keys each ref view reads (review td794121: a key meant for
    #: another view — ``offset`` on ``links``, ``limit`` on ``raw`` —
    #: used to be silently dropped).
    _VIEW_ARGS: ClassVar[dict[str, frozenset[str]]] = {
        "links": frozenset({"rel", "of_kind", "limit", "cursor"}),
        "log": frozenset({"limit", "source"}),
        "raw": frozenset({"meta_key"}),
        "lints": frozenset({"offset"}),
    }

    #: Where an args= key that isn't this view's lives — the refusal
    #: names it so the retry lands on a door that reads the key.
    _KEY_HOME: ClassVar[dict[str, str]] = {
        "rel": "view='links'",
        "of_kind": "view='links'",
        "cursor": "view='links'",
        "limit": "view='links' or view='log'",
        "source": "view='log'",
        "meta_key": "view='raw'",
        "offset": "view='lints' or a node-handle read (id='mn<N>'/'fn<N>')",
    }

    def __init__(self, *, hub: Any) -> None:
        from precis.dispatch import InitError

        if hub.store is None:
            raise InitError(f"{self.LANG.kind}: store required")
        self.store = hub.store

    # ── config-derived helpers ────────────────────────────────────────

    def _supports_bounds(self) -> bool:
        return self.LANG.default_bounds() is not None

    def _is_node_handle(self, s: str) -> bool:
        s = s.strip()
        pre = self.LANG.node_prefix
        return s.startswith(pre) and s[len(pre) :].isdigit()

    def _parse_bounds_arg(self, arg: Any) -> Any | None:
        """A ``'x y w h'`` string or ``[x,y,w,h]`` list → bounds tuple with
        ``w,h > 0``, or ``None``. Only meaningful when the kind has bounds."""
        if arg is None or not self._supports_bounds():
            return None
        if isinstance(arg, str):
            parts: list[Any] = arg.replace(",", " ").split()
        elif isinstance(arg, (list, tuple)):
            parts = list(arg)
        else:
            return None
        if len(parts) != 4:
            return None
        try:
            x, y, w, h = (float(p) for p in parts)
        except (TypeError, ValueError):
            return None
        if w <= 0 or h <= 0:
            return None
        return (x, y, w, h)

    def _box_str(self, bounds: Any) -> str:
        if isinstance(bounds, (list, tuple)) and len(bounds) == 4:
            return f"{fmt_num(bounds[2])}×{fmt_num(bounds[3])}"
        return ""

    def _ref_meta(self, bounds: Any) -> dict[str, Any]:
        meta: dict[str, Any] = {"render": self.LANG.render_value}
        if bounds is not None:
            meta[self.LANG.bounds_meta_key] = self.LANG.bounds_to_meta(bounds)
        return meta

    def _validate_source(self, text: str) -> str:
        err = self.LANG.parse_error(text)
        if err is not None:
            raise BadInput(
                f"invalid {self.LANG.medium} source: {err}",
                next="fix the syntax the parser named, then retry the same call",
            )
        try:
            return self.LANG.sanitize(text)
        except Exception as exc:  # pragma: no cover — parse_error guards
            raise BadInput(
                f"invalid {self.LANG.medium} source: {exc}",
                next="fix the part of the source named above, then retry the same call",
            ) from exc

    def _docs(self, ref_id: int) -> tuple[Any | None, Any | None, Any | None]:
        source = vocab = notes = None
        for c in self.store.drafts.reading_order(ref_id, kind=self.LANG.kind):
            if c.chunk_kind == self.LANG.source_kind and source is None:
                source = c
            elif c.chunk_kind == self.LANG.vocab_kind and vocab is None:
                vocab = c
            elif c.chunk_kind == self.LANG.notes_kind and notes is None:
                notes = c
        return source, vocab, notes

    def _resolve_live(self, slug: str) -> Any:
        # No search verb on diagram kinds: the bare-kind get is the list.
        return _resolve_live(
            self.store,
            kind=self.LANG.kind,
            id=slug,
            next_hint=f"get(kind='{self.LANG.kind}') to list existing "
            f"{self.LANG.kind}s",
        )

    def _resolve_any(self, id: str | int | None) -> Any:
        s = str(id or "").strip()
        if not s:
            raise BadInput(
                f"edit(kind='{self.LANG.kind}') requires id=",
                next=f"get(kind='{self.LANG.kind}') lists the slugs, then pass id=",
            )
        if self._is_node_handle(s):
            node = self.store.drafts.get_draft_chunk(s, kind=self.LANG.kind)
            if node is None:
                raise NotFound(
                    f"{self.LANG.kind} node {s} not found",
                    next=f"get(kind='{self.LANG.kind}', id='<slug>') lists the "
                    "current node handles",
                )
            ref = self.store.get_ref(kind=self.LANG.kind, id=int(node.ref_id))
            if ref is None:
                raise NotFound(
                    f"{self.LANG.kind} for node {s} not found",
                    next=f"get(kind='{self.LANG.kind}') for the live list — the "
                    "owning diagram may have been deleted",
                )
            return ref
        return self._resolve_live(s)

    def _resolve_project(self, project: str | int) -> int:
        raw = str(project).strip()
        raw = raw.split(":", 1)[1] if raw.startswith("todo:") else raw
        try:
            pid = int(raw)
        except ValueError as exc:
            raise BadInput(
                f"project must be a todo id, got {project!r}",
                next="project=<int todo id>",
            ) from exc
        ref = self.store.get_ref(kind="todo", id=pid)
        if ref is None:
            raise NotFound(
                f"project todo {pid} not found",
                next="search(kind='todo', q='<project name>') for the id",
            )
        return ref.id

    def _set_doc(
        self,
        ref_id: int,
        chunk: Any | None,
        chunk_kind: str,
        text: str,
        base_sha: str | None,
        *,
        index: bool,
    ) -> None:
        if chunk is None:
            self.store.drafts.add_chunks(
                ref_id=ref_id,
                chunk_kind=chunk_kind,
                text=text,
                meta=None if index else {"no_index": "true"},
                split=False,
                kind=self.LANG.kind,
            )
        else:
            self.store.drafts.edit_text(
                chunk.handle, text, base_sha=base_sha, kind=self.LANG.kind
            )

    # ── get ───────────────────────────────────────────────────────────

    def get(
        self,
        *,
        id: str | int | None = None,
        view: str | None = None,
        args: dict[str, Any] | None = None,
        **_kw: Any,
    ) -> Response:
        if id is None or (isinstance(id, str) and id.strip() in ("", "/")):
            if view is not None:
                raise BadInput(
                    f"{self.LANG.kind} view {view!r} inspects one ref — "
                    "id= is required",
                    options=list(self.spec.views),
                    next=f"get(kind='{self.LANG.kind}', id='<slug>', view='{view}')",
                )
            if args:
                # The list reads no args= key — every declared key scopes a
                # view or a node page; refuse rather than drop (review
                # td794121).
                raise BadInput(
                    f"{self.LANG.kind} list takes no args= — "
                    f"{sorted(args)} apply to a single-ref read",
                    next=f"get(kind='{self.LANG.kind}', id='<slug>', "
                    "view='links') for the inspection args, or "
                    f"get(kind='{self.LANG.kind}', id='<node handle>', "
                    "args={'offset': N}) to page one node",
                )
            return self._render_list()
        s = str(id).strip()
        if self._is_node_handle(s):
            return self._render_node(s, view=view, args=args)
        ref = self._resolve_live(s)
        if view is not None:
            return self._render_view(view, ref=ref, args=args)
        if args:
            # Every declared args= key scopes a view or a node page —
            # on a plain slug read it would silently do nothing.
            raise BadInput(
                f"{self.LANG.kind} get takes args={sorted(args)} only "
                "with a view= or a node handle",
                options=list(self.spec.views),
                next=f"get(kind='{self.LANG.kind}', id='{s}', "
                "view='links') for the inspection args, or "
                f"get(kind='{self.LANG.kind}', id='<node handle>', "
                "args={'offset': N}) to page one node",
            )
        return self._render(s, ref)

    def accepted_views(self, *, id: Any = None) -> list[str]:
        return list(self.spec.views)

    def _render_view(
        self,
        view: str,
        *,
        ref: Any,
        args: dict[str, Any] | None = None,
    ) -> Response:
        """The ref-inspection views — ``links`` (the link graph), ``log``
        (the ``ref_events`` trail), ``raw`` (stored columns + meta) and
        ``lints`` (the full finding list, paged by ``args={'offset': N}``
        — the slug render shows only a bounded preview, gr793934).

        The sections of the default render need no views of their own:
        every doc chunk prints its ``<node><id>`` handle in its section
        header, and ``get(id='<handle>')`` reads that one section whole
        (gr793934 — a fitted render's outline names the cut sections by
        those same handles).
        """
        accepted = self._VIEW_ARGS.get(view)
        if accepted is None:
            raise BadInput(
                f"unknown {self.LANG.kind} view {view!r}",
                options=list(self.spec.views),
                next=f"omit view= for the {self.LANG.kind} itself",
            )
        a = args or {}
        slug = ref.slug or "<slug>"
        extras = sorted(set(a) - accepted)
        if extras:
            # A key meant for another view silently did nothing here —
            # refuse, naming the door that reads it (review td794121).
            homes = "; ".join(
                f"{k!r} → {self._KEY_HOME.get(k, 'a different call')}" for k in extras
            )
            raise BadInput(
                f"args= keys {extras} don't apply to {self.LANG.kind} "
                f"view={view!r} — {homes}",
                options=sorted(accepted),
                next=f"get(kind='{self.LANG.kind}', id='{slug}', view='{view}')",
            )
        if view == "links":
            # The shared links table every Handler-direct kind serves
            # (``handlers/_links_render.py``); ``args={'rel'|'of_kind'}``
            # narrows, ``args={'limit'|'cursor'}`` pages (gr627629).
            from precis.handlers._links_render import render_links_view
            from precis.utils.relations import validate_relation

            rel = a.get("rel")
            if rel is not None:
                rel = validate_relation(str(rel), store=self.store)
            of_kind = a.get("of_kind")
            return render_links_view(
                self.store,
                ref,
                sense=self.LANG.kind,
                rel=rel,
                of_kind=str(of_kind) if of_kind is not None else None,
                limit=a.get("limit"),
                cursor=a.get("cursor"),
            )
        if view == "log":
            from precis.handlers._event_log_render import render_event_log

            limit = a.get("limit")
            if limit is not None:
                try:
                    limit = int(str(limit))
                except (TypeError, ValueError):
                    limit = -1
                if limit < 1:
                    raise BadInput(
                        f"view='log' limit must be a positive integer, "
                        f"got {a.get('limit')!r}",
                        next=f"get(kind='{self.LANG.kind}', id='{slug}', view='log')",
                    )
            source = a.get("source")
            return render_event_log(
                self.store,
                ref.id,
                source=str(source) if source is not None else None,
                limit=limit if limit is not None else 50,
            )
        if view == "raw":
            from precis.handlers._raw_render import render_raw

            return Response(
                body=render_raw(
                    self.store,
                    ref,
                    self.store.tags_for(ref.id),
                    meta_key=a.get("meta_key"),
                ),
                result_count=1,
            )
        # ``lints`` — every finding, paged by character offset like a
        # node read (gr793934: the slug render's preview names this
        # view for the rest).
        findings = self._lint_findings(ref)
        text = "\n".join(f"- [{f.kind}] {f.message}" for f in findings)
        if not text:
            return Response(body=f"no lint findings on {slug}", result_count=0)
        header = f"# {ref.title}  ({slug}) — {self.LANG.kind} lints"
        return self._paged_read(
            header=header,
            text=text,
            offset=a.get("offset"),
            page_call=lambda off: self._lints_page_call(slug, off),
            subject=f"{self.LANG.kind} {slug!r} lints",
            result_count=len(findings),
        )

    def _render_list(self) -> Response:
        k = self.LANG.kind
        return render_slug_ref_list(
            self.store,
            kind=k,
            label_plural=f"{k}(s)",
            empty_body=f"no {k}s yet — put(kind='{k}', id='…', title='…')",
        )

    def _node_page_call(self, handle: str, offset: int | None = None) -> str:
        """The paging call a node read's clip footer names (gr793934)."""
        extra = "" if offset is None else f", args={{'offset': {offset}}}"
        return f"get(kind='{self.LANG.kind}', id='{handle}'{extra})"

    def _lints_page_call(self, slug: str, offset: int | None = None) -> str:
        """The paging call a ``view='lints'`` clip footer names (gr793934)."""
        extra = "" if offset is None else f", args={{'offset': {offset}}}"
        return f"get(kind='{self.LANG.kind}', id='{slug}', view='lints'{extra})"

    def _paged_read(
        self,
        *,
        header: str,
        text: str,
        offset: Any,
        page_call: Callable[[int | None], str],
        subject: str,
        result_count: int | None = None,
    ) -> Response:
        """One character-offset pageable read (gr793934) — the shape
        memory's ``~body`` established (gr685357): a page over the cap
        cuts at a character boundary and its footer names the call
        continuing at the first unread character, so following the
        footers reassembles the whole text. ``page_call(None)`` is the
        unpaged read, ``page_call(N)`` the continuation at offset N.
        """
        total = len(text)
        start = 0
        if offset is not None:
            try:
                start = int(str(offset))
            except (TypeError, ValueError):
                start = -1
            if start < 0:
                raise BadInput(
                    f"offset= must be an integer >= 0, got {offset!r}",
                    next=page_call(None),
                )
            if start >= total:
                raise BadInput(
                    f"offset={start} is at or past the end of "
                    f"{subject} ({total} characters)",
                    next=page_call(None),
                )
        sliced = text[start:]
        body = (
            f"{header} (chars {start}..{total} of {total})\n\n{sliced}"
            if offset is not None
            else f"{header}\n{text}"
        )
        return Response(
            body=body,
            result_budget_hint=page_call(start if offset is not None else None),
            result_count=result_count,
            budget_fit=self._chars_page_fit(
                header=header,
                sliced=sliced,
                start=start,
                total=total,
                page_call=page_call,
                subject=subject,
            ),
        )

    def _chars_page_fit(
        self,
        *,
        header: str,
        sliced: str,
        start: int,
        total: int,
        page_call: Callable[[int | None], str],
        subject: str,
    ) -> Callable[[int], str]:
        """The ``budget_fit`` for an over-cap char-offset page.

        Sizes the part from the page's *actual* frame — the
        ``(chars S..E of T)`` header plus the ``rest:`` clip footer,
        ``E`` never wider than ``total`` — so the emitted page always
        fits the cap that bound the read (gr793934, review td794121).
        An empty part means not even one character fits beside the
        frame: refuse typed (:class:`ResultBudgetExceeded`) rather than
        emit a ``rest:`` back to the same offset — the continuation
        must always advance or not exist.
        """
        head = f"{header} (chars {start}..{total} of {total})\n\n"
        foot = (
            f"\n\n… showing chars {start}..{total} of {total}; rest: {page_call(total)}"
        )
        overhead = len(head.encode("utf-8")) + len(foot.encode("utf-8"))

        def _fit_page(budget: int) -> str:
            part = sliced.encode("utf-8")[: max(0, budget - overhead)].decode(
                "utf-8", errors="ignore"
            )
            if not part and sliced:
                raise ResultBudgetExceeded(
                    f"get: {subject} ({total} characters) cannot fit even "
                    f"one character plus its page frame under the "
                    f"{budget}-byte result budget",
                    next=page_call(start),
                )
            end = start + len(part)
            page = f"{header} (chars {start}..{end} of {total})\n\n{part}"
            if end < total:
                page += (
                    f"\n\n… showing chars {start}..{end} of {total}; "
                    f"rest: {page_call(end)}"
                )
            return page

        return _fit_page

    def _lints(self, src: str, box: Any, bindings: list[Any]) -> list[Any]:
        """Every lint finding — the source lints plus binding drift."""
        lang = self.LANG
        findings = lang.lint(src, box) if src else []
        if src and bindings:
            findings = findings + lang.lint_bindings(
                src, {b["element"] for b in bindings}
            )
        return findings

    def _lint_findings(self, ref: Any) -> list[Any]:
        """The ref's full finding list — what ``view='lints'`` pages."""
        lang = self.LANG
        source, _vocab, _notes = self._docs(ref.id)
        src = source.text if source is not None else ""
        box = lang.bounds_from_meta((ref.meta or {}).get(lang.bounds_meta_key))
        if box is None:
            box = lang.read_bounds(src)
        bindings = (
            self.store.drafts.element_bindings(source.chunk_id)
            if source is not None
            else []
        )
        return self._lints(src, box, bindings)

    def _render_node(
        self,
        handle: str,
        *,
        view: str | None = None,
        args: dict[str, Any] | None = None,
    ) -> Response:
        node = self.store.drafts.get_draft_chunk(handle, kind=self.LANG.kind)
        if node is None:
            raise NotFound(
                f"{self.LANG.kind} node {handle!r} not found",
                next=f"get(kind='{self.LANG.kind}', id='<slug>') lists the "
                "current node handles",
            )
        if view is not None:
            # Inspection views apply to the owning ref, never to one
            # chunk — name the ref so the retry is a valid call.
            owner = self.store.get_ref(kind=self.LANG.kind, id=int(node.ref_id))
            slug = owner.slug if owner is not None and owner.slug else "<slug>"
            raise BadInput(
                f"{self.LANG.kind} node {handle!r} takes no view= — "
                f"{view!r} inspects the owning {self.LANG.kind}",
                options=list(self.spec.views),
                next=f"get(kind='{self.LANG.kind}', id='{slug}', view='{view}')",
            )
        a = args or {}
        extras = sorted(set(a) - {"offset"})
        if extras:
            homes = "; ".join(
                f"{k!r} → {self._KEY_HOME.get(k, 'a different call')}" for k in extras
            )
            raise BadInput(
                f"{self.LANG.kind} node {handle!r} takes only "
                f"args={{'offset': N}} — {extras} ({homes})",
                options=["offset"],
                next=self._node_page_call(handle),
            )
        return self._paged_read(
            header=f"{node.dc}  [{node.chunk_kind}]",
            text=node.text or "",
            offset=a.get("offset"),
            page_call=lambda off: self._node_page_call(handle, off),
            subject=f"{self.LANG.kind} node {handle}",
        )

    def _render(self, slug: str, ref: Any) -> Response:
        lang = self.LANG
        source, vocab_chunk, notes_chunk = self._docs(ref.id)
        chunks = self.store.drafts.reading_order(ref.id, kind=lang.kind)
        turns = sum(c.chunk_kind == lang.turn_kind for c in chunks)
        src = source.text if source is not None else ""
        box = lang.bounds_from_meta((ref.meta or {}).get(lang.bounds_meta_key))
        if box is None:
            box = lang.read_bounds(src)
        bindings = (
            self.store.drafts.element_bindings(source.chunk_id)
            if source is not None
            else []
        )
        findings = self._lints(src, box, bindings)
        node_h = source.dc if source is not None else "—"

        tag = f" [{lang.render_value}]" if lang.render_value != lang.kind else ""
        lines = [f"# {ref.title}  ({slug}) — {lang.kind}{tag}, {turns} turn(s)"]
        line = f"source: {node_h}"
        if self._supports_bounds():
            line += f"   canvas: {self._box_str(box or lang.default_bounds())}"
        lines.append(line)
        # gr793934: the byte cap cuts from the bottom, so the load-bearing
        # parts render first — lints before everything (a dangling binding
        # must never read as clean), then the source the kind exists for.
        # Vocab / notes / bindings / elements take the cut; each doc
        # section header carries its chunk handle so a fitted outline
        # names the exact ``get(id='<handle>')`` that reads it whole.
        # The lint list itself is bounded (review td794121): an unbounded
        # one could still crowd the source out — past _LINT_PREVIEW the
        # rest live behind view='lints'.
        if findings:
            lines.append("\n## Lints")
            for f in findings[:_LINT_PREVIEW]:
                lines.append(f"- [{f.kind}] {f.message}")
            if len(findings) > _LINT_PREVIEW:
                lines.append(
                    f"- … and {len(findings) - _LINT_PREVIEW} more — "
                    f"get(kind='{lang.kind}', id='{slug}', view='lints')"
                )
        lines.append(f"\n## {lang.medium} source\n" + (src or "(empty)"))
        if vocab_chunk is not None and vocab_chunk.text.strip():
            lines.append(
                f"\n## Shared vocabulary  ({vocab_chunk.dc})\n"
                + vocab_chunk.text.strip()
            )
        if notes_chunk is not None and notes_chunk.text.strip():
            lines.append(
                f"\n## Implementation notes  ({notes_chunk.dc})\n"
                + notes_chunk.text.strip()
            )
        if bindings:
            lines.append(f"\n## Bindings ({lang.element_noun} → chunk it depicts)")
            for b in bindings:
                title = f"  {b['title']}" if b.get("title") else ""
                lines.append(
                    f"- {b['element']} → {b['handle']} ({b['relation']}){title}"
                )
        if lang.kind == "figure":
            elements = [c for c in chunks if c.chunk_kind == "figure_element"]
            if elements:
                lines.append("\n## Top-level elements")
                for chunk in elements:
                    key = chunk.meta["element_key"].split(":", 2)[-1]
                    lines.append(f"- {chunk.dc}  {key.rsplit('}', 1)[-1]}")
        return Response(
            body="\n".join(lines),
            # The fitted read's ``next:`` names the paged source-node read
            # when there is one — ``args={'offset': N}`` walks it to the
            # end (gr793934: a source over the cap is still whole across
            # the continuation chain).
            result_budget_hint=(
                f"get(kind='{lang.kind}', id='{node_h}') pages the "
                f"{lang.medium} source (its footer names "
                f"args={{'offset': N}}); get(kind='{lang.kind}', "
                f"id='<node handle>') for another node, or "
                f"get(kind='{lang.kind}') to list the {lang.kind}s"
                if source is not None
                else f"get(kind='{lang.kind}') to list the {lang.kind}s"
            ),
        )

    # ── put ────────────────────────────────────────────────────────────

    def put(
        self,
        *,
        id: str | int | None = None,
        title: str | None = None,
        project: str | int | None = None,
        viewbox: Any = None,
        text: str | None = None,
        vocab: str | None = None,
        **_kw: Any,
    ) -> Response:
        lang = self.LANG
        if id is None or not str(id).strip():
            raise BadInput(
                f"put(kind='{lang.kind}') requires id= (the {lang.kind} slug)",
                next=f"put(kind='{lang.kind}', id='x', title='X')",
            )
        slug = str(id).strip()
        if self.store.get_ref(kind=lang.kind, id=slug) is not None:
            raise BadInput(
                f"{lang.kind} {slug!r} already exists",
                next=f"edit it: edit(kind='{lang.kind}', id='{slug}', text='…')",
            )
        box = self._parse_bounds_arg(viewbox)
        if box is None:
            box = lang.default_bounds()
        source = self._validate_source(text) if text else lang.default_source(box)
        # A supplied source's own bounds win (it's the content).
        from_src = lang.read_bounds(source)
        if from_src is not None:
            box = from_src

        ref = self.store.insert_ref(
            kind=lang.kind,
            slug=slug,
            title=(title or slug).strip() or slug,
            meta=self._ref_meta(box),
        )
        self.store.drafts.add_chunks(
            ref_id=ref.id,
            chunk_kind=lang.source_kind,
            text=source,
            meta={"no_index": "true"},
            split=False,
            kind=lang.kind,
        )
        # The vocab doc is born only when explicitly seeded; otherwise it stays
        # empty until the model writes it. Notes are always born empty (lazy).
        if vocab and vocab.strip():
            self.store.drafts.add_chunks(
                ref_id=ref.id,
                chunk_kind=lang.vocab_kind,
                text=vocab.strip(),
                split=False,
                kind=lang.kind,
            )
        linked = ""
        if project is not None:
            pid = self._resolve_project(project)
            self.store.add_link(
                src_ref_id=ref.id, dst_ref_id=pid, relation=lang.project_relation
            )
            linked = f"; linked {lang.project_relation} project {pid}"
        canvas = ""
        if self._supports_bounds() and box is not None:
            canvas = f"; canvas {self._box_str(box)}"
        return Response(
            body=f"created {lang.kind} '{slug}' ({lang.ref_prefix}{ref.id})"
            f"{canvas}{linked}"
        )

    # ── edit ───────────────────────────────────────────────────────────

    def edit(
        self,
        *,
        id: str | int | None = None,
        text: str | None = None,
        vocab: str | None = None,
        notes: str | None = None,
        viewbox: Any = None,
        base_sha: str | None = None,
        **_kw: Any,
    ) -> Response:
        lang = self.LANG
        ref = self._resolve_any(id)
        source, vocab_chunk, notes_chunk = self._docs(ref.id)

        if viewbox is not None:
            if not self._supports_bounds():
                raise BadInput(
                    f"{lang.kind} has no viewBox to set",
                    next=f"edit(kind='{lang.kind}', id='<slug>', text='…') instead — "
                    "the source declares the canvas",
                )
            box = self._parse_bounds_arg(viewbox)
            if box is None:
                raise BadInput(
                    "viewbox must be 'x y w h' with w,h > 0",
                    next="viewbox='0 0 256 256'",
                )
            self.store.stamp_ref_meta(
                ref.id, {lang.bounds_meta_key: lang.bounds_to_meta(box)}
            )
            return Response(body=f"viewBox → {self._box_str(box)} on {ref.slug}")

        if vocab is not None:
            if not str(vocab).strip():
                raise BadInput(
                    "edit vocab= must be non-empty",
                    next=f"edit(kind='{lang.kind}', id='<slug>', vocab='<vocabulary text>')",
                )
            self._set_doc(
                ref.id, vocab_chunk, lang.vocab_kind, str(vocab), base_sha, index=True
            )
            return Response(body=f"updated shared vocabulary on {ref.slug}")

        if notes is not None:
            if not str(notes).strip():
                raise BadInput(
                    "edit notes= must be non-empty",
                    next=f"edit(kind='{lang.kind}', id='<slug>', notes='<note text>')",
                )
            self._set_doc(
                ref.id, notes_chunk, lang.notes_kind, str(notes), base_sha, index=False
            )
            return Response(body=f"updated implementation notes on {ref.slug}")

        if text is not None:
            clean = self._validate_source(text)
            if source is None:
                self.store.drafts.add_chunks(
                    ref_id=ref.id,
                    chunk_kind=lang.source_kind,
                    text=clean,
                    meta={"no_index": "true"},
                    split=False,
                    kind=lang.kind,
                )
            else:
                self.store.drafts.edit_text(
                    source.handle, clean, base_sha=base_sha, kind=lang.kind
                )
            box = lang.read_bounds(clean)
            if box is not None:
                self.store.stamp_ref_meta(
                    ref.id, {lang.bounds_meta_key: lang.bounds_to_meta(box)}
                )
            findings = lang.lint(clean, box)
            note = f" — {len(findings)} lint(s)" if findings else ""
            return Response(body=f"set {lang.medium} source on {ref.slug}{note}")

        raise BadInput(
            f"edit(kind='{lang.kind}') needs text= (source), vocab= (shared "
            "vocabulary), or notes= (implementation notes)"
            + (", or viewbox=" if self._supports_bounds() else ""),
            next=f"edit(kind='{lang.kind}', id='<slug>', text='…')",
        )

    # ── delete ─────────────────────────────────────────────────────────

    def delete(self, *, id: str | int | None = None, **_kw: Any) -> Response:
        if id is None or not str(id).strip():
            raise BadInput(
                f"delete(kind='{self.LANG.kind}') requires id=",
                next=f"get(kind='{self.LANG.kind}') lists the slugs, then "
                f"delete(kind='{self.LANG.kind}', id='<slug>')",
            )
        ref = self._resolve_live(str(id).strip())
        self.store.retire_ref(ref.id)
        return Response(body=f"retired {self.LANG.kind} {ref.slug}")

    # ── link: folder placement + element→chunk binding (0057) ──

    def link(  # type: ignore[override]
        self,
        *,
        id: str | int,
        target: str | None = None,
        mode: str = "add",
        rel: str | None = None,
        element: str | None = None,
        **_kw: Any,
    ) -> Response:
        from precis.handlers._placement import RESERVED_PARENT_REL, place_ref

        if element is not None:
            return self._link_element(id=id, element=element, target=target, mode=mode)

        if rel == RESERVED_PARENT_REL:
            ref = self._resolve_live(str(id).strip())
            return place_ref(
                self.store, kind=self.LANG.kind, ref=ref, target=target, mode=mode
            )
        raise BadInput(
            f"{self.LANG.kind} link supports rel='parent' (folder placement) or "
            f"element=<{self.LANG.element_noun} id> + target=<chunk handle> "
            "(bind it to the chunk it depicts)",
            next=f"link(kind='{self.LANG.kind}', id='<slug>', element='x', target='dc42')",
        )

    def _link_element(
        self, *, id: str | int, element: str, target: str | None, mode: str
    ) -> Response:
        lang = self.LANG
        ref = self._resolve_any(id)
        source, _v, _n = self._docs(ref.id)
        if source is None:
            raise BadInput(
                f"{lang.kind} {ref.slug} has no source yet — nothing to bind to",
                next=f"edit(kind='{lang.kind}', id='{ref.slug}', text='…')",
            )
        if mode == "remove":
            n = self.store.drafts.unbind_element(
                node_chunk_id=source.chunk_id, element=element, target=target
            )
            return Response(
                body=f"unbound {lang.element_noun} {element!r} on {ref.slug} "
                f"({n} edge(s))"
            )
        if not target or not str(target).strip():
            raise BadInput(
                f"binding a {lang.element_noun} needs target= (the chunk handle "
                "it depicts)",
                next=f"link(kind='{lang.kind}', id='<slug>', element='x', target='dc42')",
            )
        self.store.drafts.bind_element(
            node_chunk_id=source.chunk_id,
            element=element,
            target=str(target).strip(),
            relation="depicts",
        )
        return Response(
            body=f"bound {lang.element_noun} {element!r} → {str(target).strip()} "
            f"(depicts) on {ref.slug}"
        )
