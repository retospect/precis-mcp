"""FigureHandler — the interactive SVG-canvas kind (migration 0057).

A ``figure`` is a slug-addressed ref on the **same** chunk-tree substrate as
``draft`` / ``plan`` (the :class:`~precis.store._draft_ops.DraftMixin` ops,
parameterised ``kind='figure'``), but a **distinct kind** so it is never
exported as a corpus deliverable (``corpus_role='none'``). It holds two
model-owned documents — the SVG **source** (a ``figure_node`` chunk,
``fn<id>``) and the shared **vocabulary** (a ``figure_vocab`` chunk) — plus a
``figure_turn`` chat log. Unlike ``plan``'s 1:1 ``plan-of``, a project may
own *many* figures (``figure-of``), so creation uses ``insert_ref`` +
``add_chunks`` directly rather than ``create_draft``'s 1:1 dup-checked path.

This is the MCP surface (get / put / edit / delete / link). The *interactive*
draw-with-me turn loop lives in :func:`precis.figure.turn.run_turn`, driven
by the web editor (:mod:`precis_web.routes.figure`) — not an MCP verb.
"""

from __future__ import annotations

import logging
import re
from typing import Any, ClassVar

from precis.dispatch import Hub, InitError
from precis.errors import BadInput, NotFound
from precis.figure.svg import (
    DEFAULT_VIEWBOX,
    SvgError,
    default_svg,
    lint_bindings,
    lint_svg,
    parse_error,
    read_viewbox,
    sanitize_svg,
)
from precis.handlers._slug_ref_shared import render_slug_ref_list, resolve_live_slug_ref
from precis.protocol import Handler, KindSpec
from precis.response import Response

log = logging.getLogger(__name__)

#: A figure source-node address — the universal handle ``fn<chunk_id>``.
_FIGURE_NODE_ADDR_RE = re.compile(r"^fn\d+$")

# NB: the vocab / notes docs are born EMPTY (lazy-created on first write), not
# seeded with placeholder prose. The explanation of what each doc is *for* is
# instruction, not content, so it lives in the turn prompt + the
# `precis-figure-svg` skill — never in the stored doc (a stored seed would be
# shown to the human as if it were real content and the model would have to
# work around it). The SVG source is different: its `default_svg()` is real
# content (a valid empty canvas the browser must render), so that DOES seed.


def _is_node_addr(s: str) -> bool:
    return bool(_FIGURE_NODE_ADDR_RE.match(s.strip()))


def _parse_viewbox(
    vb: Any,
) -> tuple[float, float, float, float] | None:
    """Coerce a ``[x,y,w,h]`` list or ``"x y w h"`` string to a tuple, or None."""
    if vb is None:
        return None
    parts: list[Any]
    if isinstance(vb, str):
        parts = vb.replace(",", " ").split()
    elif isinstance(vb, (list, tuple)):
        parts = list(vb)
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


class FigureHandler(Handler):
    spec: ClassVar[KindSpec] = KindSpec(
        kind="figure",
        title="Figure",
        description=(
            "An interactive SVG canvas you draw *with* the model. put creates "
            "a figure (id=<slug>, title=, optional project=<todo>, optional "
            "viewbox='0 0 W H' or text=<svg>); get lists / renders the figure "
            "(assembled SVG + shared vocabulary + fn<id> source handle + "
            "lints) / reads a node fn<id>; edit sets the SVG source (text=), "
            "the shared vocabulary (vocab=), the implementation notes (notes="
            "— the model's private design log), or the viewBox (viewbox=); delete "
            "soft-retires the figure. The interactive draw-with-me chat is in "
            "the /figure web editor. corpus_role=none (never exported). See "
            "precis-figure-help."
        ),
        supports_get=True,
        supports_put=True,
        supports_edit=True,
        supports_delete=True,
        supports_link=True,
        is_numeric=False,
        id_required=False,
        note_like=True,
        role="artifact",
        corpus_role="none",
    )

    def __init__(self, *, hub: Hub) -> None:
        if hub.store is None:
            raise InitError("figure: store required")
        self.store = hub.store

    # ── get ──────────────────────────────────────────────────────────

    def get(  # type: ignore[override]
        self, *, id: str | int | None = None, view: str | None = None, **_kw: Any
    ) -> Response:
        if id is None or (isinstance(id, str) and id.strip() in ("", "/")):
            return self._render_list()
        s = str(id).strip()
        if view is not None:
            raise BadInput(
                f"unknown figure view {view!r}", next="omit view= for the figure"
            )
        if _is_node_addr(s):
            return self._render_node(s)
        ref = resolve_live_slug_ref(self.store, kind="figure", id=s)
        return self._render_figure(s, ref)

    # ── put: create a figure ─────────────────────────────────────────

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
        if id is None or not str(id).strip():
            raise BadInput(
                "put(kind='figure') requires id= (the figure slug)",
                next="put(kind='figure', id='mascot', title='Mascot')",
            )
        slug = str(id).strip()
        if self.store.get_ref(kind="figure", id=slug) is not None:
            raise BadInput(
                f"figure {slug!r} already exists",
                next=f"edit it: edit(kind='figure', id='{slug}', text='<svg>…')",
            )
        box = _parse_viewbox(viewbox) or DEFAULT_VIEWBOX
        source_svg = self._validate_source(text) if text else default_svg(box)
        # A supplied source's own viewBox wins (it's the content).
        box = read_viewbox(source_svg) or box

        ref = self.store.insert_ref(
            kind="figure",
            slug=slug,
            title=(title or slug).strip() or slug,
            meta={"render": "svg", "viewbox": list(box)},
        )
        self.store.add_chunks(
            ref_id=ref.id,
            chunk_kind="figure_node",
            text=source_svg,
            meta={"no_index": "true"},
            split=False,
            kind="figure",
        )
        # The vocab doc is born only when there's real content for it (an
        # explicit vocab=); otherwise it stays empty until the model writes it.
        # Notes are always born empty (lazy). See the note above the constants.
        if vocab and vocab.strip():
            self.store.add_chunks(
                ref_id=ref.id,
                chunk_kind="figure_vocab",
                text=vocab.strip(),
                split=False,
                kind="figure",
            )
        linked = ""
        if project is not None:
            pid = self._resolve_project(project)
            self.store.add_link(src_ref_id=ref.id, dst_ref_id=pid, relation="figure-of")
            linked = f"; linked figure-of project {pid}"
        return Response(
            body=f"created figure '{slug}' (fg{ref.id}); canvas {self._box_str(box)}{linked}"
        )

    # ── edit: source / vocabulary / viewBox ──────────────────────────

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
        ref = self._resolve_any(id)
        source, vocab_chunk, notes_chunk = self._docs(ref.id)

        if viewbox is not None:
            box = _parse_viewbox(viewbox)
            if box is None:
                raise BadInput(
                    "viewbox must be 'x y w h' with w,h > 0",
                    next="viewbox='0 0 256 256'",
                )
            self.store.stamp_ref_meta(ref.id, {"viewbox": list(box)})
            return Response(body=f"viewBox → {self._box_str(box)} on {ref.slug}")

        if vocab is not None:
            if not str(vocab).strip():
                raise BadInput("edit vocab= must be non-empty")
            self._set_doc(
                ref.id, vocab_chunk, "figure_vocab", str(vocab), base_sha, index=True
            )
            return Response(body=f"updated shared vocabulary on {ref.slug}")

        if notes is not None:
            if not str(notes).strip():
                raise BadInput("edit notes= must be non-empty")
            self._set_doc(
                ref.id, notes_chunk, "figure_notes", str(notes), base_sha, index=False
            )
            return Response(body=f"updated implementation notes on {ref.slug}")

        if text is not None:
            clean = self._validate_source(text)
            if source is None:
                self.store.add_chunks(
                    ref_id=ref.id,
                    chunk_kind="figure_node",
                    text=clean,
                    meta={"no_index": "true"},
                    split=False,
                    kind="figure",
                )
            else:
                self.store.edit_text(
                    source.handle, clean, base_sha=base_sha, kind="figure"
                )
            box = read_viewbox(clean)
            if box is not None:
                self.store.stamp_ref_meta(ref.id, {"viewbox": list(box)})
            findings = lint_svg(clean, box)
            note = f" — {len(findings)} lint(s)" if findings else ""
            return Response(body=f"set SVG source on {ref.slug}{note}")

        raise BadInput(
            "edit(kind='figure') needs text= (SVG source), vocab= (shared "
            "vocabulary), notes= (implementation notes), or viewbox=",
            next="edit(kind='figure', id='<slug>', text='<svg>…')",
        )

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
        """Create-or-replace a figure's vocab / notes prose chunk. ``index``
        False mints ``meta.no_index`` (notes are internal, never searched)."""
        if chunk is None:
            self.store.add_chunks(
                ref_id=ref_id,
                chunk_kind=chunk_kind,
                text=text,
                meta=None if index else {"no_index": "true"},
                split=False,
                kind="figure",
            )
        else:
            self.store.edit_text(chunk.handle, text, base_sha=base_sha, kind="figure")

    # ── delete: soft-retire the figure ───────────────────────────────

    def delete(  # type: ignore[override]
        self, *, id: str | int | None = None, **_kw: Any
    ) -> Response:
        if id is None or not str(id).strip():
            raise BadInput("delete(kind='figure') requires id= (the figure slug)")
        ref = resolve_live_slug_ref(self.store, kind="figure", id=str(id).strip())
        self.store.soft_delete_ref(ref.id)
        return Response(body=f"retired figure {ref.slug}")

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

        # Element→chunk binding (ADR 0057): element= names a stable id in the
        # SVG source; target= the chunk it depicts. mode='remove' unbinds.
        if element is not None:
            return self._link_element(id=id, element=element, target=target, mode=mode)

        if rel == RESERVED_PARENT_REL:
            ref = resolve_live_slug_ref(self.store, kind="figure", id=str(id).strip())
            return place_ref(
                self.store, kind="figure", ref=ref, target=target, mode=mode
            )
        raise BadInput(
            "figure link supports rel='parent' (folder placement) or "
            "element=<id> + target=<chunk handle> (bind an element to the "
            "chunk it depicts, ADR 0057)",
            next="link(kind='figure', id='<slug>', element='hook', target='dc42')",
        )

    def _link_element(
        self, *, id: str | int, element: str, target: str | None, mode: str
    ) -> Response:
        """Bind (or unbind) an SVG element to the chunk it depicts."""
        ref = self._resolve_any(id)
        source, _v, _n = self._docs(ref.id)
        if source is None:
            raise BadInput(
                f"figure {ref.slug} has no SVG source yet — nothing to bind to",
                next=f"edit(kind='figure', id='{ref.slug}', text='<svg>…')",
            )
        if mode == "remove":
            n = self.store.unbind_element(
                node_chunk_id=source.chunk_id, element=element, target=target
            )
            return Response(
                body=f"unbound element {element!r} on {ref.slug} ({n} edge(s))"
            )
        if not target or not str(target).strip():
            raise BadInput(
                "binding an element needs target= (the chunk handle it depicts)",
                next="link(kind='figure', id='<slug>', element='hook', target='dc42')",
            )
        self.store.bind_element(
            node_chunk_id=source.chunk_id,
            element=element,
            target=str(target).strip(),
            relation="depicts",
        )
        return Response(
            body=f"bound element {element!r} → {str(target).strip()} "
            f"(depicts) on {ref.slug}"
        )

    # ── helpers ──────────────────────────────────────────────────────

    def _validate_source(self, text: str) -> str:
        """Compile-check + sanitize a supplied SVG source, or raise BadInput."""
        err = parse_error(text)
        if err is not None:
            raise BadInput(f"invalid SVG source: {err}")
        try:
            return sanitize_svg(text)
        except SvgError as exc:  # pragma: no cover — parse_error already guards
            raise BadInput(f"invalid SVG source: {exc}") from exc

    def _docs(self, ref_id: int) -> tuple[Any | None, Any | None, Any | None]:
        """Return ``(source_chunk, vocab_chunk, notes_chunk)`` for a figure."""
        source = vocab = notes = None
        for c in self.store.reading_order(ref_id, kind="figure"):
            if c.chunk_kind == "figure_node" and source is None:
                source = c
            elif c.chunk_kind == "figure_vocab" and vocab is None:
                vocab = c
            elif c.chunk_kind == "figure_notes" and notes is None:
                notes = c
        return source, vocab, notes

    def _resolve_any(self, id: str | int | None) -> Any:
        """Resolve a figure ref from its slug or an ``fn<id>`` node address."""
        s = str(id or "").strip()
        if not s:
            raise BadInput("edit(kind='figure') requires id=")
        if _is_node_addr(s):
            node = self.store.get_draft_chunk(s, kind="figure")
            if node is None:
                raise NotFound(f"figure node {s} not found")
            ref = self.store.get_ref(kind="figure", id=int(node.ref_id))
            if ref is None:
                raise NotFound(f"figure for node {s} not found")
            return ref
        return resolve_live_slug_ref(self.store, kind="figure", id=s)

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

    @staticmethod
    def _box_str(box: tuple[float, float, float, float]) -> str:
        def n(v: float) -> str:
            return str(int(v)) if v == int(v) else str(v)

        return f"{n(box[2])}×{n(box[3])}"

    def _render_list(self) -> Response:
        return render_slug_ref_list(
            self.store,
            kind="figure",
            label_plural="figure(s)",
            empty_body="no figures yet — put(kind='figure', id='…', title='…')",
        )

    def _render_figure(self, slug: str, ref: Any) -> Response:
        source, vocab_chunk, notes_chunk = self._docs(ref.id)
        turns = sum(
            1
            for c in self.store.reading_order(ref.id, kind="figure")
            if c.chunk_kind == "figure_turn"
        )
        svg = source.text if source is not None else ""
        box = _parse_viewbox((ref.meta or {}).get("viewbox")) or read_viewbox(svg)
        bindings = (
            self.store.element_bindings(source.chunk_id) if source is not None else []
        )
        findings = lint_svg(svg, box) if svg else []
        if svg and bindings:
            findings = findings + lint_bindings(svg, {b["element"] for b in bindings})
        node_h = source.dc if source is not None else "—"

        lines = [f"# {ref.title}  ({slug}) — figure [svg], {turns} turn(s)"]
        lines.append(
            f"source: {node_h}   canvas: {self._box_str(box or DEFAULT_VIEWBOX)}"
        )
        if vocab_chunk is not None and vocab_chunk.text.strip():
            lines.append("\n## Shared vocabulary\n" + vocab_chunk.text.strip())
        if notes_chunk is not None and notes_chunk.text.strip():
            lines.append("\n## Implementation notes\n" + notes_chunk.text.strip())
        if bindings:
            lines.append("\n## Bindings (element → chunk it depicts)")
            for b in bindings:
                title = f"  {b['title']}" if b.get("title") else ""
                lines.append(
                    f"- {b['element']} → {b['handle']} ({b['relation']}){title}"
                )
        lines.append("\n## SVG source\n" + (svg or "(empty)"))
        if findings:
            lines.append("\n## Lints")
            lines.extend(f"- [{f.kind}] {f.message}" for f in findings)
        return Response(body="\n".join(lines))

    def _render_node(self, addr: str) -> Response:
        node = self.store.get_draft_chunk(addr, kind="figure")
        if node is None:
            raise NotFound(f"figure node {addr!r} not found")
        return Response(body=f"{node.dc}  [{node.chunk_kind}]\n{node.text}")
