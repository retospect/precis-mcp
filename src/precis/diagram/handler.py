"""``DiagramHandler`` — the MCP surface (get/put/edit/delete/link) shared by
every diagram kind (ADR 0057, slice 4 factoring).

A diagram kind is a slug-addressed ref on the ``draft`` chunk-tree substrate
(the :class:`~precis.store._draft_ops.DraftMixin` ops, parameterised by the
kind), never exported (``corpus_role='none'``). The CRUD is identical across
languages — create the ref + source chunk (+ optional vocab), edit the source /
vocab / notes (/ bounds), render source + vocab + notes + node→chunk bindings +
lints, bind a source element to the chunk it depicts — differing only in the
per-kind config carried on :class:`~precis.diagram.lang.DiagramLang` (the
handle scheme, the source language mechanics, the display nouns, and whether
the kind has a bounds/viewBox axis).

Concrete handlers are thin: they set ``LANG`` + a ``spec`` and inherit
everything here. ``figure`` (SVG, with a viewBox) and ``mermaid`` (auto-layout,
no bounds) are the two instances.
"""

from __future__ import annotations

from typing import Any, ClassVar

from precis.diagram.lang import DiagramLang
from precis.errors import BadInput, NotFound
from precis.handlers._slug_ref_shared import render_slug_ref_list, resolve_live_slug_ref
from precis.protocol import Handler
from precis.response import Response


def _num(v: float) -> str:
    return str(int(v)) if v == int(v) else str(v)


class DiagramHandler(Handler):
    #: Set by each concrete subclass — the source language + per-kind config.
    LANG: ClassVar[DiagramLang]

    def __init__(self, *, hub: Any) -> None:
        from precis.dispatch import InitError

        if hub.store is None:
            raise InitError(f"{self.LANG.kind}: store required")
        self.store = hub.store

    # ── config-derived helpers ────────────────────────────────────────

    def _supports_bounds(self) -> bool:
        return self.LANG.default_bounds() is not None

    def _is_node_addr(self, s: str) -> bool:
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
            return f"{_num(bounds[2])}×{_num(bounds[3])}"
        return ""

    def _ref_meta(self, bounds: Any) -> dict[str, Any]:
        meta: dict[str, Any] = {"render": self.LANG.render_value}
        if bounds is not None:
            meta[self.LANG.bounds_meta_key] = self.LANG.bounds_to_meta(bounds)
        return meta

    def _validate_source(self, text: str) -> str:
        err = self.LANG.parse_error(text)
        if err is not None:
            raise BadInput(f"invalid {self.LANG.medium} source: {err}")
        try:
            return self.LANG.sanitize(text)
        except Exception as exc:  # pragma: no cover — parse_error guards
            raise BadInput(f"invalid {self.LANG.medium} source: {exc}") from exc

    def _docs(self, ref_id: int) -> tuple[Any | None, Any | None, Any | None]:
        source = vocab = notes = None
        for c in self.store.reading_order(ref_id, kind=self.LANG.kind):
            if c.chunk_kind == self.LANG.source_kind and source is None:
                source = c
            elif c.chunk_kind == self.LANG.vocab_kind and vocab is None:
                vocab = c
            elif c.chunk_kind == self.LANG.notes_kind and notes is None:
                notes = c
        return source, vocab, notes

    def _resolve_any(self, id: str | int | None) -> Any:
        s = str(id or "").strip()
        if not s:
            raise BadInput(f"edit(kind='{self.LANG.kind}') requires id=")
        if self._is_node_addr(s):
            node = self.store.get_draft_chunk(s, kind=self.LANG.kind)
            if node is None:
                raise NotFound(f"{self.LANG.kind} node {s} not found")
            ref = self.store.get_ref(kind=self.LANG.kind, id=int(node.ref_id))
            if ref is None:
                raise NotFound(f"{self.LANG.kind} for node {s} not found")
            return ref
        return resolve_live_slug_ref(self.store, kind=self.LANG.kind, id=s)

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
            raise NotFound(f"project todo {pid} not found")
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
            self.store.add_chunks(
                ref_id=ref_id,
                chunk_kind=chunk_kind,
                text=text,
                meta=None if index else {"no_index": "true"},
                split=False,
                kind=self.LANG.kind,
            )
        else:
            self.store.edit_text(
                chunk.handle, text, base_sha=base_sha, kind=self.LANG.kind
            )

    # ── get ───────────────────────────────────────────────────────────

    def get(  # type: ignore[override]
        self, *, id: str | int | None = None, view: str | None = None, **_kw: Any
    ) -> Response:
        if id is None or (isinstance(id, str) and id.strip() in ("", "/")):
            return self._render_list()
        s = str(id).strip()
        if view is not None:
            raise BadInput(
                f"unknown {self.LANG.kind} view {view!r}",
                next=f"omit view= for the {self.LANG.kind}",
            )
        if self._is_node_addr(s):
            return self._render_node(s)
        ref = resolve_live_slug_ref(self.store, kind=self.LANG.kind, id=s)
        return self._render(s, ref)

    def _render_list(self) -> Response:
        k = self.LANG.kind
        return render_slug_ref_list(
            self.store,
            kind=k,
            label_plural=f"{k}(s)",
            empty_body=f"no {k}s yet — put(kind='{k}', id='…', title='…')",
        )

    def _render_node(self, addr: str) -> Response:
        node = self.store.get_draft_chunk(addr, kind=self.LANG.kind)
        if node is None:
            raise NotFound(f"{self.LANG.kind} node {addr!r} not found")
        return Response(body=f"{node.dc}  [{node.chunk_kind}]\n{node.text}")

    def _render(self, slug: str, ref: Any) -> Response:
        lang = self.LANG
        source, vocab_chunk, notes_chunk = self._docs(ref.id)
        turns = sum(
            1
            for c in self.store.reading_order(ref.id, kind=lang.kind)
            if c.chunk_kind == lang.turn_kind
        )
        src = source.text if source is not None else ""
        box = lang.bounds_from_meta((ref.meta or {}).get(lang.bounds_meta_key))
        if box is None:
            box = lang.read_bounds(src)
        bindings = (
            self.store.element_bindings(source.chunk_id) if source is not None else []
        )
        findings = lang.lint(src, box) if src else []
        if src and bindings:
            findings = findings + lang.lint_bindings(
                src, {b["element"] for b in bindings}
            )
        node_h = source.dc if source is not None else "—"

        tag = f" [{lang.render_value}]" if lang.render_value != lang.kind else ""
        lines = [f"# {ref.title}  ({slug}) — {lang.kind}{tag}, {turns} turn(s)"]
        line = f"source: {node_h}"
        if self._supports_bounds():
            line += f"   canvas: {self._box_str(box or lang.default_bounds())}"
        lines.append(line)
        if vocab_chunk is not None and vocab_chunk.text.strip():
            lines.append("\n## Shared vocabulary\n" + vocab_chunk.text.strip())
        if notes_chunk is not None and notes_chunk.text.strip():
            lines.append("\n## Implementation notes\n" + notes_chunk.text.strip())
        if bindings:
            lines.append(f"\n## Bindings ({lang.element_noun} → chunk it depicts)")
            for b in bindings:
                title = f"  {b['title']}" if b.get("title") else ""
                lines.append(
                    f"- {b['element']} → {b['handle']} ({b['relation']}){title}"
                )
        lines.append(f"\n## {lang.medium} source\n" + (src or "(empty)"))
        if findings:
            lines.append("\n## Lints")
            lines.extend(f"- [{f.kind}] {f.message}" for f in findings)
        return Response(body="\n".join(lines))

    # ── put ────────────────────────────────────────────────────────────

    def put(  # type: ignore[override]
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
        self.store.add_chunks(
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
            self.store.add_chunks(
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

    def edit(  # type: ignore[override]
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
                raise BadInput(f"{lang.kind} has no viewBox to set")
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
                raise BadInput("edit vocab= must be non-empty")
            self._set_doc(
                ref.id, vocab_chunk, lang.vocab_kind, str(vocab), base_sha, index=True
            )
            return Response(body=f"updated shared vocabulary on {ref.slug}")

        if notes is not None:
            if not str(notes).strip():
                raise BadInput("edit notes= must be non-empty")
            self._set_doc(
                ref.id, notes_chunk, lang.notes_kind, str(notes), base_sha, index=False
            )
            return Response(body=f"updated implementation notes on {ref.slug}")

        if text is not None:
            clean = self._validate_source(text)
            if source is None:
                self.store.add_chunks(
                    ref_id=ref.id,
                    chunk_kind=lang.source_kind,
                    text=clean,
                    meta={"no_index": "true"},
                    split=False,
                    kind=lang.kind,
                )
            else:
                self.store.edit_text(
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

    def delete(  # type: ignore[override]
        self, *, id: str | int | None = None, **_kw: Any
    ) -> Response:
        if id is None or not str(id).strip():
            raise BadInput(f"delete(kind='{self.LANG.kind}') requires id=")
        ref = resolve_live_slug_ref(self.store, kind=self.LANG.kind, id=str(id).strip())
        self.store.soft_delete_ref(ref.id)
        return Response(body=f"retired {self.LANG.kind} {ref.slug}")

    # ── link: folder placement (ADR 0045) + element→chunk binding (0057) ──

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
            ref = resolve_live_slug_ref(
                self.store, kind=self.LANG.kind, id=str(id).strip()
            )
            return place_ref(
                self.store, kind=self.LANG.kind, ref=ref, target=target, mode=mode
            )
        raise BadInput(
            f"{self.LANG.kind} link supports rel='parent' (folder placement) or "
            f"element=<{self.LANG.element_noun} id> + target=<chunk handle> "
            "(bind it to the chunk it depicts, ADR 0057)",
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
            n = self.store.unbind_element(
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
        self.store.bind_element(
            node_chunk_id=source.chunk_id,
            element=element,
            target=str(target).strip(),
            relation="depicts",
        )
        return Response(
            body=f"bound {lang.element_noun} {element!r} → {str(target).strip()} "
            f"(depicts) on {ref.slug}"
        )
