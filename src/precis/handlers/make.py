"""MakeHandler — the make-tree kind (assembly / synthesis order).

A ``make`` tree is process, not structure: the order things come together
(`docs/backlog/make-tree-vs-design-tree.md` — the EBOM/MBOM split). It
rides the **same** chunk-tree substrate as ``draft``/``plan``
(:class:`~precis.store._draft_ops.DraftStore`, ``store.drafts``), so step
nodes are first-class, ordered, hierarchical, and addressed by the stable
handle ``mk<chunk_id>`` — which survives text edits and reordering, so a
``made-by`` link (design block → step, written from the *cad* side)
never dangles. Each step's ``meta`` carries its conditions (fixture,
torque, work center; reagents, temperature) — the reusable know-how the
design tree cannot hold.

Deliberately NOT a ``plan``: no ``project=`` todo binding, no ``plan-of``
edge, no cursor/belief — so the plan_tick machinery never mistakes an
assembly procedure for a thread's reasoning outline. Step ``status``
(open/wip/done) is kept: a build in progress is a todo list.

Two make-orders may exist over the same design (placed assembly vs bulk
synthesis) — each is its own ``make`` ref; the design links to each.
"""

from __future__ import annotations

import logging
import re
from typing import Any, ClassVar

from precis.dispatch import Hub, InitError
from precis.errors import BadInput, NotFound
from precis.handlers._slug_ref_shared import (
    render_slug_ref_list,
    resolve_live_slug_ref,
)
from precis.protocol import Handler, KindSpec
from precis.response import Response

log = logging.getLogger(__name__)

_MAKE_CHUNK_HANDLE_RE = re.compile(r"^mk\d+(?:[+\-^].*|\.\..*)?$")

_STATUS_MARKERS: dict[str, str] = {"open": "[open]", "wip": "[wip]", "done": "done:"}
_VALID_STATUS = tuple(_STATUS_MARKERS)

#: Step meta keys rendered inline as conditions (everything except the
#: status marker); free-form — conditions vocabulary hardens with use.
_MARKER_KEYS = ("status",)

_GLOSS_CAP = 100


def _cap(text: str, n: int = _GLOSS_CAP) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= n else flat[: n - 1].rstrip() + "…"


def _is_make_chunk_handle(s: str) -> bool:
    return bool(_MAKE_CHUNK_HANDLE_RE.match(s.strip()))


class MakeHandler(Handler):
    spec: ClassVar[KindSpec] = KindSpec(
        kind="make",
        title="Make",
        description=(
            "A make-tree: assembly/synthesis ORDER for a design — process, "
            "not structure (the design tree keeps what a thing IS; a make "
            "tree keeps how it comes together, and the two need not align). "
            "put creates (id=slug, title=) or adds a step (text=, at={first|"
            "last|into|before|after}, status=open|wip|done, meta={fixture, "
            "torque, reagents, …} — the step's conditions); get lists / "
            "renders the step tree with each step's attached blocks / reads "
            "one step (id='mk<id>'); edit changes text, moves (move=), or "
            "sets status=; delete soft-retires. Steps are addressed "
            "mk<chunk_id>. Blocks attach from the design side: "
            "link(kind='cad', id=<design>, target='make:<slug>' (whole tree) "
            "or 'mk<id>' (one step), rel='made-by'). See precis-cad-help."
        ),
        supports_get=True,
        supports_put=True,
        supports_edit=True,
        supports_delete=True,
        supports_link=True,
        is_numeric=False,
        id_required=False,
        note_like=True,
        placement="artifact",
        corpus_role="none",
        views=("links",),
    )

    def __init__(self, *, hub: Hub) -> None:
        if hub.store is None:
            raise InitError("make: store required")
        self.store = hub.store

    # ── link: placement only (made-by edges are written cad-side) ────

    def link(  # type: ignore[override]
        self,
        *,
        id: str | int,
        target: str | None = None,
        mode: str = "add",
        rel: str | None = None,
        **_kw: Any,
    ) -> Response:
        from precis.handlers._placement import RESERVED_PARENT_REL, place_ref

        if rel == RESERVED_PARENT_REL:
            ref = resolve_live_slug_ref(self.store, kind="make", id=str(id).strip())
            return place_ref(self.store, kind="make", ref=ref, target=target, mode=mode)
        raise BadInput(
            "make link supports only rel='parent' (folder placement) — "
            "made-by edges are written from the design side",
            next=(
                "link(kind='cad', id='<design>', target='mk<id>', "
                "rel='made-by') aligns a block to a step"
            ),
        )

    # ── get ──────────────────────────────────────────────────────────

    def get(
        self, *, id: str | int | None = None, view: str | None = None, **_kw: Any
    ) -> Response:
        if id is None or (isinstance(id, str) and id.strip() in ("", "/")):
            return render_slug_ref_list(
                self.store,
                kind="make",
                label_plural="make tree(s)",
                empty_body="no make trees yet — put(kind='make', id='…', title='…')",
            )
        s = str(id).strip()
        if _is_make_chunk_handle(s):
            node = self.store.drafts.get_draft_chunk(s, kind="make")
            if node is None:
                raise NotFound(f"make step {s!r} not found")
            meta = {k: v for k, v in (node.meta or {}).items() if v is not None}
            body = f"{node.dc} [{meta.pop('status', 'open')}] {node.text}"
            if meta:
                body += "\n" + "  ".join(f"{k}={v}" for k, v in sorted(meta.items()))
            return Response(body=body)
        ref = resolve_live_slug_ref(self.store, kind="make", id=s)
        if view == "links":
            from precis.handlers._links_render import render_links_view

            return render_links_view(self.store, ref, sense="make")
        if view is not None:
            raise BadInput(
                f"unknown make view {view!r}",
                next="omit view= for the step tree; view='links' for the graph",
            )
        return self._render_tree(s, ref)

    def _render_tree(self, slug: str, ref: Any) -> Response:
        """The whole ordered step tree, one line per step, with each step's
        conditions and its attached blocks (incoming ``made-by`` links) —
        the alignment is many-to-many and this is where you read it."""
        chunks = self.store.drafts.reading_order(ref.id, kind="make")
        title_id = chunks[0].chunk_id if chunks else None
        blocks = self._blocks_by_step(ref)
        n = max(len(chunks) - 1, 0)
        lines = [f"# {ref.title}  ({slug}) — {n} step{'' if n == 1 else 's'}\n"]
        for c in chunks:
            if c.chunk_id == title_id:
                made = ", ".join(blocks.get(None, []))
                if made:
                    lines[0] = lines[0].rstrip() + f"\nmakes: {made}\n"
                continue
            meta = {k: v for k, v in (c.meta or {}).items() if v is not None}
            status = str(meta.pop("status", "open"))
            marker = _STATUS_MARKERS.get(status, _STATUS_MARKERS["open"])
            cond = "  ".join(f"{k}={v}" for k, v in sorted(meta.items()))
            made = ", ".join(blocks.get(c.chunk_id, []))
            line = f"{'  ' * c.depth}{marker} {c.dc} {_cap(c.text or '')}"
            if cond:
                line += f"  ⟨{cond}⟩"
            if made:
                line += f"  ⛓ {made}"
            lines.append(line.rstrip())
        return Response(body="\n".join(lines))

    def _blocks_by_step(self, ref: Any) -> dict[int | None, list[str]]:
        """Incoming ``made-by`` sources grouped by step chunk (``None`` =
        the ref-level "this design is made by this tree" edge)."""
        out: dict[int | None, list[str]] = {}
        try:
            links = self.store.links_for(ref.id, direction="in", relation="made-by")
        except Exception:  # pragma: no cover - render is best-effort
            return out
        if not links:
            return out
        with self.store.pool.connection() as conn:
            rows = conn.execute(
                """SELECT r.ref_id, r.kind,
                          COALESCE((SELECT id_value FROM ref_identifiers
                                     WHERE ref_id = r.ref_id
                                       AND id_kind = 'cite_key'
                                     ORDER BY created_at DESC LIMIT 1),
                                   r.ref_id::text)
                     FROM refs r WHERE r.ref_id = ANY(%s)""",
                ([lk.src_ref_id for lk in links],),
            ).fetchall()
        names = {int(rid): f"{kind}:{slug}" for rid, kind, slug in rows}
        for lk in links:
            key = lk.dst_chunk_id if lk.dst_chunk_id is not None else None
            out.setdefault(key, []).append(names.get(lk.src_ref_id, str(lk.src_ref_id)))
        return out

    # ── put: create a tree, or add a step ────────────────────────────

    def put(
        self,
        *,
        id: str | int | None = None,
        text: str | None = None,
        title: str | None = None,
        at: dict[str, Any] | None = None,
        status: str | None = None,
        meta: dict[str, Any] | None = None,
        **_kw: Any,
    ) -> Response:
        if id is None or not str(id).strip():
            raise BadInput(
                "put(kind='make') requires id= (the make-tree slug)",
                next="put(kind='make', id='crane-assembly', title='crane assembly order')",
            )
        slug = str(id).strip()
        existing = self.store.get_ref(kind="make", id=slug)
        if existing is None:
            ref, title_chunk = self.store.drafts.create_draft(
                name=slug,
                title=(title or slug).strip() or slug,
                kind="make",
            )
            extra = ""
            if text is not None and str(text).strip():
                added = self._add_steps(ref, text, at, status, meta)
                extra = f"; added step {' '.join(c.dc for c in added)}"
            return Response(
                body=(
                    f"created make tree '{slug}' (root {title_chunk.dc}){extra}. "
                    "Align blocks from the design side: link(kind='cad', "
                    f"id='<design>', target='make:{slug}', rel='made-by')"
                )
            )
        if text is None or not str(text).strip():
            raise BadInput(
                f"make tree {slug!r} exists — adding a step requires text=",
                next=(
                    f"put(kind='make', id={slug!r}, text='press bearing into "
                    "bore', at={'last': True}, meta={'fixture': 'arbor press'})"
                ),
            )
        added = self._add_steps(existing, text, at, status, meta)
        n = len(added)
        return Response(
            body=(
                f"added {n} step{'' if n == 1 else 's'} to {slug}: "
                + " ".join(c.dc for c in added)
            )
        )

    def _add_steps(
        self,
        ref: Any,
        text: str,
        at: dict[str, Any] | None,
        status: str | None,
        meta: dict[str, Any] | None,
    ) -> list[Any]:
        node_meta = dict(meta or {})
        if status is not None:
            st = str(status).strip()
            if st not in _VALID_STATUS:
                raise BadInput(
                    f"status must be one of {list(_VALID_STATUS)}",
                    next="status='open' | 'wip' | 'done'",
                )
            node_meta["status"] = st
        return self.store.drafts.add_chunks(
            ref_id=ref.id,
            chunk_kind="step",
            text=str(text),
            at=self._resolve_at_anchors(at),
            meta=node_meta,
            kind="make",
        )

    # ── edit / delete ────────────────────────────────────────────────

    def edit(
        self,
        *,
        id: str | int | None = None,
        text: str | None = None,
        move: dict[str, Any] | None = None,
        status: str | None = None,
        base_sha: str | None = None,
        **_kw: Any,
    ) -> Response:
        handle = self._require_step(id, verb="edit")
        base = self.store.drafts.get_draft_chunk(handle, kind="make")
        if base is None:
            raise NotFound(f"make step {handle!r} not found")
        internal = base.handle
        if move is not None:
            resolved = self._resolve_at_anchors(move)
            assert resolved is not None
            c = self.store.drafts.move_chunk(internal, resolved, kind="make")
            return Response(body=f"moved {(c or base).dc}")
        if status is not None:
            st = str(status).strip()
            if st and st not in _VALID_STATUS:
                raise BadInput(f"status must be one of {list(_VALID_STATUS)}")
            self.store.drafts.patch_chunk_meta(internal, {"status": st or None})
            return Response(body=f"marked {base.dc} [{st or 'open'}]")
        if text is not None:
            if not str(text).strip():
                raise BadInput("edit text= must be non-empty")
            c = self.store.drafts.edit_text(
                internal, str(text), base_sha=base_sha, kind="make"
            )
            return Response(body=f"edited {(c or base).dc}")
        raise BadInput(
            "edit(kind='make') requires text= (rewrite), move= "
            "(reorder/reparent), or status= (set the step state)",
            next="edit(kind='make', id='mk<id>', text='…')",
        )

    def delete(
        self, *, id: str | int | None = None, mode: str | None = None, **_kw: Any
    ) -> Response:
        s = str(id or "").strip()
        if _is_make_chunk_handle(s):
            node = self.store.drafts.get_draft_chunk(s, kind="make")
            if node is None:
                raise NotFound(f"make step {s!r} not found")
            self.store.drafts.retire_chunk(node.handle, mode=mode, kind="make")
            return Response(body=f"retired {node.dc}")
        ref = resolve_live_slug_ref(self.store, kind="make", id=s)
        self.store.retire_ref(ref.id)
        return Response(body=f"retired make tree {s!r}")

    # ── helpers ──────────────────────────────────────────────────────

    def _require_step(self, id: str | int | None, *, verb: str) -> str:
        if id is None or not _is_make_chunk_handle(str(id)):
            raise BadInput(
                f"{verb}(kind='make') targets a step — id='mk<chunk_id>'",
                next=f"{verb}(kind='make', id='mk42', …)",
            )
        return str(id)

    def _resolve_at_anchors(self, at: dict[str, Any] | None) -> dict[str, Any] | None:
        """Rewrite ``mk<id>`` anchors in an ``at=``/``move=`` intent to the
        internal handle the store mutator keys on."""
        if not at:
            return at
        out = dict(at)
        for key in ("before", "after", "into"):
            anchor = out.get(key)
            if anchor is not None and _is_make_chunk_handle(str(anchor)):
                node = self.store.drafts.get_draft_chunk(str(anchor), kind="make")
                if node is None:
                    raise NotFound(f"at: no make step {anchor!r}")
                out[key] = node.handle
        return out
