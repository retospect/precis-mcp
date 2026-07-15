"""MermaidHandler — the mermaid diagram kind (migration 0066, ADR 0057).

A ``mermaid`` is a slug-addressed ref on the **same** chunk-tree substrate as
``figure`` / ``draft`` (the :class:`~precis.store._draft_ops.DraftMixin` ops,
parameterised ``kind='mermaid'``), a **distinct kind** never exported
(``corpus_role='none'``). It holds the model-owned mermaid **source** (a
``mermaid_node`` chunk, ``mn<id>``) + the shared **vocabulary** + private
**notes**, plus a ``mermaid_turn`` chat log — and its nodes bind to the chunks
they depict (ADR 0057). Validation / render / export go through the pure-Python
``mermaidx`` engine via :data:`precis.mermaid.MERMAID_LANG`.

This is the MCP surface (get / put / edit / delete / link). The interactive
draw-with-me turn loop is the shared :func:`precis.diagram.turn.run_turn`
driven by the ``/mermaid`` web editor. Ships dark behind
``PRECIS_MERMAID_ENABLED``.
"""

from __future__ import annotations

import logging
import re
from typing import Any, ClassVar

from precis.dispatch import Hub, InitError
from precis.errors import BadInput, NotFound
from precis.handlers._slug_ref_shared import render_slug_ref_list, resolve_live_slug_ref
from precis.mermaid import MERMAID_LANG as LANG
from precis.protocol import Handler, KindSpec
from precis.response import Response

log = logging.getLogger(__name__)

#: A mermaid source-node address — the universal handle ``mn<chunk_id>``.
_NODE_ADDR_RE = re.compile(r"^mn\d+$")


def _is_node_addr(s: str) -> bool:
    return bool(_NODE_ADDR_RE.match(s.strip()))


class MermaidHandler(Handler):
    spec: ClassVar[KindSpec] = KindSpec(
        kind="mermaid",
        title="Mermaid",
        description=(
            "A mermaid diagram you draw *with* the model (flowchart / sequence "
            "/ state / class …). put creates one (id=<slug>, title=, optional "
            "project=<todo>, or text=<mermaid source>); get lists / renders "
            "(source + shared vocabulary + mn<id> handle + node→chunk bindings "
            "+ lints) / reads a node mn<id>; edit sets the source (text=), the "
            "shared vocabulary (vocab=), or the implementation notes (notes=); "
            "link binds a node to the chunk it depicts (element=<node id>, "
            "target=<dc…/pc…/me…>); delete soft-retires. The interactive "
            "draw-with-me chat is the /mermaid web editor. corpus_role=none "
            "(never exported). See precis-mermaid-help."
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
            raise InitError("mermaid: store required")
        self.store = hub.store

    # ── get ──────────────────────────────────────────────────────────

    def get(  # type: ignore[override]
        self, *, id: str | int | None = None, view: str | None = None, **_kw: Any
    ) -> Response:
        if id is None or (isinstance(id, str) and id.strip() in ("", "/")):
            return render_slug_ref_list(
                self.store,
                kind="mermaid",
                label_plural="mermaid diagram(s)",
                empty_body="no mermaid diagrams yet — put(kind='mermaid', id='…', title='…')",
            )
        s = str(id).strip()
        if view is not None:
            raise BadInput(
                f"unknown mermaid view {view!r}", next="omit view= for the diagram"
            )
        if _is_node_addr(s):
            return self._render_node(s)
        ref = resolve_live_slug_ref(self.store, kind="mermaid", id=s)
        return self._render(s, ref)

    # ── put ───────────────────────────────────────────────────────────

    def put(  # type: ignore[override]
        self,
        *,
        id: str | int | None = None,
        title: str | None = None,
        project: str | int | None = None,
        text: str | None = None,
        vocab: str | None = None,
        **_kw: Any,
    ) -> Response:
        if id is None or not str(id).strip():
            raise BadInput(
                "put(kind='mermaid') requires id= (the diagram slug)",
                next="put(kind='mermaid', id='flow', title='Pipeline')",
            )
        slug = str(id).strip()
        if self.store.get_ref(kind="mermaid", id=slug) is not None:
            raise BadInput(
                f"mermaid {slug!r} already exists",
                next=f"edit it: edit(kind='mermaid', id='{slug}', text='<source>')",
            )
        source = self._validate_source(text) if text else LANG.default_source(None)
        ref = self.store.insert_ref(
            kind="mermaid",
            slug=slug,
            title=(title or slug).strip() or slug,
            meta={"render": "mermaid"},
        )
        self.store.add_chunks(
            ref_id=ref.id,
            chunk_kind=LANG.source_kind,
            text=source,
            meta={"no_index": "true"},
            split=False,
            kind="mermaid",
        )
        if vocab and vocab.strip():
            self.store.add_chunks(
                ref_id=ref.id,
                chunk_kind=LANG.vocab_kind,
                text=vocab.strip(),
                split=False,
                kind="mermaid",
            )
        linked = ""
        if project is not None:
            pid = self._resolve_project(project)
            self.store.add_link(
                src_ref_id=ref.id, dst_ref_id=pid, relation="mermaid-of"
            )
            linked = f"; linked mermaid-of project {pid}"
        return Response(body=f"created mermaid '{slug}' (mm{ref.id}){linked}")

    # ── edit ──────────────────────────────────────────────────────────

    def edit(  # type: ignore[override]
        self,
        *,
        id: str | int | None = None,
        text: str | None = None,
        vocab: str | None = None,
        notes: str | None = None,
        base_sha: str | None = None,
        **_kw: Any,
    ) -> Response:
        ref = self._resolve_any(id)
        source, vocab_chunk, notes_chunk = self._docs(ref.id)

        if vocab is not None:
            if not str(vocab).strip():
                raise BadInput("edit vocab= must be non-empty")
            self._set_doc(
                ref.id, vocab_chunk, LANG.vocab_kind, str(vocab), base_sha, index=True
            )
            return Response(body=f"updated shared vocabulary on {ref.slug}")

        if notes is not None:
            if not str(notes).strip():
                raise BadInput("edit notes= must be non-empty")
            self._set_doc(
                ref.id, notes_chunk, LANG.notes_kind, str(notes), base_sha, index=False
            )
            return Response(body=f"updated implementation notes on {ref.slug}")

        if text is not None:
            clean = self._validate_source(text)
            if source is None:
                self.store.add_chunks(
                    ref_id=ref.id,
                    chunk_kind=LANG.source_kind,
                    text=clean,
                    meta={"no_index": "true"},
                    split=False,
                    kind="mermaid",
                )
            else:
                self.store.edit_text(
                    source.handle, clean, base_sha=base_sha, kind="mermaid"
                )
            findings = LANG.lint(clean, None)
            note = f" — {len(findings)} lint(s)" if findings else ""
            return Response(body=f"set mermaid source on {ref.slug}{note}")

        raise BadInput(
            "edit(kind='mermaid') needs text= (source), vocab= (shared "
            "vocabulary), or notes= (implementation notes)",
            next="edit(kind='mermaid', id='<slug>', text='flowchart TD…')",
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
        if chunk is None:
            self.store.add_chunks(
                ref_id=ref_id,
                chunk_kind=chunk_kind,
                text=text,
                meta=None if index else {"no_index": "true"},
                split=False,
                kind="mermaid",
            )
        else:
            self.store.edit_text(chunk.handle, text, base_sha=base_sha, kind="mermaid")

    # ── delete ────────────────────────────────────────────────────────

    def delete(  # type: ignore[override]
        self, *, id: str | int | None = None, **_kw: Any
    ) -> Response:
        if id is None or not str(id).strip():
            raise BadInput("delete(kind='mermaid') requires id= (the diagram slug)")
        ref = resolve_live_slug_ref(self.store, kind="mermaid", id=str(id).strip())
        self.store.soft_delete_ref(ref.id)
        return Response(body=f"retired mermaid {ref.slug}")

    # ── link: parent placement (ADR 0045) + node→chunk binding (0057) ──

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
            ref = resolve_live_slug_ref(self.store, kind="mermaid", id=str(id).strip())
            return place_ref(
                self.store, kind="mermaid", ref=ref, target=target, mode=mode
            )
        raise BadInput(
            "mermaid link supports rel='parent' (folder placement) or "
            "element=<node id> + target=<chunk handle> (bind a node to the "
            "chunk it depicts, ADR 0057)",
            next="link(kind='mermaid', id='<slug>', element='intake', target='dc42')",
        )

    def _link_element(
        self, *, id: str | int, element: str, target: str | None, mode: str
    ) -> Response:
        ref = self._resolve_any(id)
        source, _v, _n = self._docs(ref.id)
        if source is None:
            raise BadInput(
                f"mermaid {ref.slug} has no source yet — nothing to bind to",
                next=f"edit(kind='mermaid', id='{ref.slug}', text='flowchart TD…')",
            )
        if mode == "remove":
            n = self.store.unbind_element(
                node_chunk_id=source.chunk_id, element=element, target=target
            )
            return Response(
                body=f"unbound node {element!r} on {ref.slug} ({n} edge(s))"
            )
        if not target or not str(target).strip():
            raise BadInput(
                "binding a node needs target= (the chunk handle it depicts)",
                next="link(kind='mermaid', id='<slug>', element='intake', target='dc42')",
            )
        self.store.bind_element(
            node_chunk_id=source.chunk_id,
            element=element,
            target=str(target).strip(),
            relation="depicts",
        )
        return Response(
            body=f"bound node {element!r} → {str(target).strip()} (depicts) on {ref.slug}"
        )

    # ── helpers ──────────────────────────────────────────────────────

    def _validate_source(self, text: str) -> str:
        """Compile-check + sanitize a supplied mermaid source, or raise."""
        err = LANG.parse_error(text)
        if err is not None:
            raise BadInput(f"invalid mermaid source: {err}")
        return LANG.sanitize(text)

    def _docs(self, ref_id: int) -> tuple[Any | None, Any | None, Any | None]:
        source = vocab = notes = None
        for c in self.store.reading_order(ref_id, kind="mermaid"):
            if c.chunk_kind == LANG.source_kind and source is None:
                source = c
            elif c.chunk_kind == LANG.vocab_kind and vocab is None:
                vocab = c
            elif c.chunk_kind == LANG.notes_kind and notes is None:
                notes = c
        return source, vocab, notes

    def _resolve_any(self, id: str | int | None) -> Any:
        s = str(id or "").strip()
        if not s:
            raise BadInput("edit(kind='mermaid') requires id=")
        if _is_node_addr(s):
            node = self.store.get_draft_chunk(s, kind="mermaid")
            if node is None:
                raise NotFound(f"mermaid node {s} not found")
            ref = self.store.get_ref(kind="mermaid", id=int(node.ref_id))
            if ref is None:
                raise NotFound(f"mermaid for node {s} not found")
            return ref
        return resolve_live_slug_ref(self.store, kind="mermaid", id=s)

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

    def _render(self, slug: str, ref: Any) -> Response:
        source, vocab_chunk, notes_chunk = self._docs(ref.id)
        turns = sum(
            1
            for c in self.store.reading_order(ref.id, kind="mermaid")
            if c.chunk_kind == LANG.turn_kind
        )
        src = source.text if source is not None else ""
        bindings = (
            self.store.element_bindings(source.chunk_id) if source is not None else []
        )
        findings = LANG.lint(src, None) if src else []
        if src and bindings:
            findings = findings + LANG.lint_bindings(
                src, {b["element"] for b in bindings}
            )
        node_h = source.dc if source is not None else "—"

        lines = [f"# {ref.title}  ({slug}) — mermaid, {turns} turn(s)"]
        lines.append(f"source: {node_h}")
        if vocab_chunk is not None and vocab_chunk.text.strip():
            lines.append("\n## Shared vocabulary\n" + vocab_chunk.text.strip())
        if notes_chunk is not None and notes_chunk.text.strip():
            lines.append("\n## Implementation notes\n" + notes_chunk.text.strip())
        if bindings:
            lines.append("\n## Bindings (node → chunk it depicts)")
            for b in bindings:
                title = f"  {b['title']}" if b.get("title") else ""
                lines.append(
                    f"- {b['element']} → {b['handle']} ({b['relation']}){title}"
                )
        lines.append("\n## Mermaid source\n" + (src or "(empty)"))
        if findings:
            lines.append("\n## Lints")
            lines.extend(f"- [{f.kind}] {f.message}" for f in findings)
        return Response(body="\n".join(lines))

    def _render_node(self, addr: str) -> Response:
        node = self.store.get_draft_chunk(addr, kind="mermaid")
        if node is None:
            raise NotFound(f"mermaid node {addr!r} not found")
        return Response(body=f"{node.dc}  [{node.chunk_kind}]\n{node.text}")
