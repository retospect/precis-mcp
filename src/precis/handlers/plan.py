"""PlanHandler — the reasoning-outline kind (ADR 0051 §2b).

A ``plan`` is a slug-addressed ref whose body chunks are a hierarchical
**todo-list + reasoning notes** — the *forward* facet of a thread's
logbook. It rides the **same** chunk-tree substrate as ``draft`` (the
mutable ``handle``/``pos``/``parent_chunk_id``/``content_sha``/
``retired_at`` columns + ``chunk_events``, added by migration 0031) and
the same :class:`~precis.store._draft_ops.DraftMixin` store ops — but it
is a **distinct kind** so it is *never* exported as the deliverable
(``corpus_role='none'``; the export guard rejects it).

Unlike ``draft`` this handler is deliberately **lean**: no figures /
tables / authors / styles / glossary / word-targets / export machinery.
A plan node carries only its text plus two markers in ``meta``:

- ``status`` ∈ ``{open, wip, done}`` — the todo-list state (default
  ``open``); rendered ``[open]`` / ``[wip]`` / ``done:``.
- ``belief`` ∈ ``{?, ⚠}`` — an uncertainty / caution flag, prefixed to
  the marker when set.

The plan is rendered **whole** every turn (``get(id='<slug>')``) with a
``▸`` you-are-here **cursor** — a model-owned pointer stored as
``meta.cursor = 'pe<id>'`` on the *plan ref* (not on any chunk). Nodes
are addressed by the ADR 0036 universal handle ``pe<chunk_id>`` (with
relative nav ``pe<id>^`` / ``+N`` / ``-lo..hi``). See ``precis-overview``.
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
from precis.store._draft_ops import content_sha
from precis.utils import handle_registry

log = logging.getLogger(__name__)

#: A bare plan-chunk address — the ADR 0036 universal handle ``pe<chunk_id>``,
#: optionally with a relative operator (``^`` / ``+`` / ``-`` / ``..``). Used
#: to tell a chunk address from a plan slug in ``get`` / ``edit`` / ``delete``.
_PLAN_CHUNK_ADDR_RE = re.compile(r"^pe\d+(?:[+\-^].*|\.\..*)?$")

#: Accepted node ``status`` values (the todo-list state) → rendered marker.
_STATUS_MARKERS: dict[str, str] = {"open": "[open]", "wip": "[wip]", "done": "done:"}
_VALID_STATUS = tuple(_STATUS_MARKERS)
#: Accepted ``belief`` flags (uncertainty / caution).
_VALID_BELIEF = ("?", "⚠")

#: Outline is one-line-per-node; a plan node is meant to be a short todo, but a
#: model *will* sometimes write a paragraph into one. Cap the whole-tree gloss
#: so a prose body can't blow out the render's one-line invariant (the full
#: text is still readable via ``get(id='pe<id>')``).
_GLOSS_CAP = 100


def _cap(text: str, n: int = _GLOSS_CAP) -> str:
    """Collapse whitespace and clip to ``n`` chars with a … ellipsis."""
    flat = " ".join(text.split())
    return flat if len(flat) <= n else flat[: n - 1].rstrip() + "…"


def _is_plan_chunk_addr(s: str) -> bool:
    """True iff ``s`` addresses a plan chunk (``pe<id>``, optionally with a
    relative operator)."""
    return bool(_PLAN_CHUNK_ADDR_RE.match(s.strip()))


class PlanHandler(Handler):
    spec: ClassVar[KindSpec] = KindSpec(
        kind="plan",
        title="Plan",
        description=(
            "A thread's reasoning outline (ADR 0051 §2b) — a hierarchical "
            "todo-list + notes on the draft chunk-tree substrate, rendered "
            "whole and NEVER exported. put creates a plan (project=, title=) "
            "or adds a node (chunk_kind=, text=, at={first|last|into|before|"
            "after}, status=open|wip|done, belief=?|⚠); get lists / renders "
            "the whole marked tree / reads a node window pe<id>-B+A; edit "
            "changes text, moves (move=), sets status=/belief=, or sets the "
            "▸ cursor (cursor='pe<id>'); delete soft-retires (mode=cascade|"
            "promote). Nodes addressed by pe<chunk_id>. See precis-overview."
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
        views=("toc",),
    )

    def __init__(self, *, hub: Hub) -> None:
        if hub.store is None:
            raise InitError("plan: store required")
        self.store = hub.store

    # ── link: placement only (ADR 0045) ─────────────────────────────

    def link(  # type: ignore[override]
        self,
        *,
        id: str | int,
        target: str | None = None,
        mode: str = "add",
        rel: str | None = None,
        **_kw: Any,
    ) -> Response:
        """Folder placement via the reserved virtual ``rel='parent'`` — the
        only accepted relation (a ``refs.parent_id`` write into a
        ``kind='folder'`` container, ADR 0045), mirroring draft."""
        from precis.handlers._placement import RESERVED_PARENT_REL, place_ref

        if rel == RESERVED_PARENT_REL:
            ref = resolve_live_slug_ref(self.store, kind="plan", id=str(id).strip())
            return place_ref(self.store, kind="plan", ref=ref, target=target, mode=mode)
        raise BadInput(
            "plan link supports only rel='parent' (folder placement)",
            next="link(kind='plan', id='<slug>', target='folder:N', rel='parent')",
        )

    # ── get ──────────────────────────────────────────────────────────

    def get(  # type: ignore[override]
        self, *, id: str | int | None = None, view: str | None = None, **_kw: Any
    ) -> Response:
        if id is None or (isinstance(id, str) and id.strip() in ("", "/")):
            return self._render_list()
        s = str(id).strip()
        if _is_plan_chunk_addr(s):
            return self._render_chunk(s)
        ref = resolve_live_slug_ref(self.store, kind="plan", id=s)
        if view is not None and view != "toc":
            raise BadInput(
                f"unknown plan view {view!r}",
                next="omit view= for the whole marked outline (view='toc' == the tree)",
            )
        return self._render_outline(s, ref)

    # ── put: create a plan, or add a node ────────────────────────────

    def put(  # type: ignore[override]
        self,
        *,
        id: str | int | None = None,
        text: str | None = None,
        title: str | None = None,
        project: str | int | None = None,
        chunk_kind: str | None = None,
        at: dict[str, Any] | None = None,
        status: str | None = None,
        belief: str | None = None,
        meta: dict[str, Any] | None = None,
        **_kw: Any,
    ) -> Response:
        if id is None or not str(id).strip():
            raise BadInput(
                "put(kind='plan') requires id= (the plan slug)",
                next="put(kind='plan', id='nanotrans-plan', title='…', project=<todo-id>)",
            )
        slug = str(id).strip()
        create_mode = str(_kw.get("mode") or "").strip().lower() == "create"

        # Create vs. add-node. `project=` is the create-only param (the owning
        # project todo) and `mode='create'` is the explicit signal — either one
        # means "create", even alongside title=/text= (the natural "make me a
        # plan" call). Only a node placement (chunk_kind/at/text) WITHOUT those
        # adds a node to an existing plan. Routing on `text` alone caused the
        # chicken-and-egg: a create call carrying text= was misrouted into the
        # lookup below and hit a misleading "plan slug not found".
        wants_node = chunk_kind is not None or at is not None or text is not None
        if wants_node and project is None and not create_mode:
            try:
                ref = resolve_live_slug_ref(self.store, kind="plan", id=slug)
            except NotFound:
                raise BadInput(
                    f"plan {slug!r} doesn't exist yet — create it before adding "
                    "nodes (a create needs project=, the owning project todo id)",
                    next=(
                        f"put(kind='plan', id={slug!r}, title='…', project=<todo-id>)"
                    ),
                ) from None
            if text is None or not str(text).strip():
                raise BadInput(
                    "adding a plan node requires text=",
                    next=(
                        "put(kind='plan', id='nanotrans-plan', text='draft the "
                        "intro', at={'last': True}, status='open')"
                    ),
                )
            node_meta = dict(meta or {})
            node_meta.update(self._marker_patch(status, belief, clear=False))
            chunks = self.store.add_chunks(
                ref_id=ref.id,
                chunk_kind=chunk_kind or "paragraph",
                text=str(text),
                at=self._resolve_at_anchors(at),
                meta=node_meta,
                kind="plan",
            )
            handles = " ".join(c.dc for c in chunks)
            n = len(chunks)
            return Response(
                body=f"added {n} node{'' if n == 1 else 's'} to {slug}: {handles}"
            )

        # else: create the plan
        if project is None:
            raise BadInput(
                "creating a plan requires project= (the owning project todo id)",
                next="put(kind='plan', id='nanotrans-plan', title='…', project=<todo-id>)",
            )
        project_ref_id = self._resolve_project(project)
        ref, title_chunk = self.store.create_draft(
            name=slug,
            title=(title or slug).strip() or slug,
            project_ref_id=project_ref_id,
            meta=meta,
            kind="plan",
            relation="plan-of",
        )
        # A create call that also carried text= meant "start the plan with this
        # first thought" — seed it as a node so the text isn't silently dropped.
        extra = ""
        if text is not None and str(text).strip():
            node_meta = dict(meta or {})
            node_meta.update(self._marker_patch(status, belief, clear=False))
            node_chunks = self.store.add_chunks(
                ref_id=ref.id,
                chunk_kind=chunk_kind or "paragraph",
                text=str(text),
                at=self._resolve_at_anchors(at),
                meta=node_meta,
                kind="plan",
            )
            extra = f"; added node {' '.join(c.dc for c in node_chunks)}"
        return Response(
            body=(
                f"created plan '{slug}' (root {title_chunk.dc}); "
                f"linked plan-of project {project_ref_id}{extra}"
            )
        )

    # ── edit: text / move / markers / cursor ─────────────────────────

    def edit(  # type: ignore[override]
        self,
        *,
        id: str | int | None = None,
        text: str | None = None,
        move: dict[str, Any] | None = None,
        status: str | None = None,
        belief: str | None = None,
        cursor: str | None = None,
        base_sha: str | None = None,
        **_kw: Any,
    ) -> Response:
        # ``cursor`` is a plan-level op (a model-owned pointer on the plan
        # REF, not a chunk) — id is the plan slug (or any node in it).
        if cursor is not None:
            return self._set_cursor(id, cursor)
        handle = self._require_chunk_id(id, verb="edit")
        base = self.store.get_draft_chunk(handle, kind="plan")
        if base is None:
            raise NotFound(f"plan node {handle!r} not found")
        internal = base.handle
        if move is not None:
            move = self._resolve_move_anchors(move)
            c = self.store.move_chunk(internal, move, kind="plan")
            return Response(body=f"moved {(c or base).dc}")
        if status is not None or belief is not None:
            patch = self._marker_patch(status, belief, clear=True)
            self.store.patch_chunk_meta(internal, patch)
            c = self.store.get_draft_chunk(internal, kind="plan")
            return Response(body=f"marked {(c or base).dc} {self._marker(c or base)}")
        if text is not None:
            if not str(text).strip():
                raise BadInput("edit text= must be non-empty")
            c = self.store.edit_text(
                internal, str(text), base_sha=base_sha, kind="plan"
            )
            return Response(body=f"edited {(c or base).dc}")
        raise BadInput(
            "edit(kind='plan') requires text= (rewrite), move= (reorder/reparent), "
            "status=/belief= (set markers), or cursor= (set the ▸ you-are-here)",
            next="edit(kind='plan', id='pe<id>', text='…')",
        )

    def _set_cursor(self, id: str | int | None, cursor: str) -> Response:
        """Set the plan's ``▸`` cursor — ``meta.cursor`` on the plan ref.
        Model-owned: it points at the node the thread is 'here' on. ``cursor``
        must resolve to a live node in *this* plan; ``''`` clears it."""
        ref = self._resolve_plan_any(id)
        target = str(cursor).strip()
        if target:
            node = self.store.get_draft_chunk(target, kind="plan")
            if node is None:
                raise NotFound(f"cursor target {target!r} is not a plan node")
            if int(node.ref_id) != ref.id:
                raise BadInput(
                    f"cursor target {target!r} is not a node in plan {ref.slug or ref.id}"
                )
            target = node.dc  # canonicalise to pe<id>
        # ``meta || {..}`` is merge-only (can't delete a key), so a clear
        # stores JSON null — the outline treats ``cursor: null`` as unset and
        # falls back to the first open node.
        self.store.stamp_ref_meta(ref.id, {"cursor": target or None})
        if target:
            return Response(body=f"cursor → {target} on {ref.slug or ref.id}")
        return Response(body=f"cleared cursor on {ref.slug or ref.id}")

    # ── delete: soft-retire ──────────────────────────────────────────

    def delete(  # type: ignore[override]
        self,
        *,
        id: str | int | None = None,
        mode: str | None = None,
        **_kw: Any,
    ) -> Response:
        handle = self._require_chunk_id(id, verb="delete")
        node = self.store.get_draft_chunk(handle, kind="plan")
        if node is None:
            raise NotFound(f"plan node {handle!r} not found")
        self.store.retire_chunk(node.handle, mode=mode, kind="plan")
        return Response(body=f"retired {node.dc}")

    # ── helpers ──────────────────────────────────────────────────────

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

    def _resolve_plan_any(self, id: str | int | None) -> Any:
        """Resolve a plan ref from either its slug or a ``pe<id>`` node."""
        s = str(id or "").strip()
        if _is_plan_chunk_addr(s):
            node = self.store.get_draft_chunk(s, kind="plan")
            if node is None:
                raise NotFound(f"plan node {s} not found")
            ref = self.store.get_ref(kind="plan", id=int(node.ref_id))
            if ref is None:
                raise NotFound(f"plan for node {s} not found")
            return ref
        return resolve_live_slug_ref(self.store, kind="plan", id=s)

    def _require_chunk_id(self, id: str | int | None, *, verb: str) -> str:
        if id is None or not _is_plan_chunk_addr(str(id)):
            raise BadInput(
                f"{verb}(kind='plan') targets a node — id='pe<chunk_id>'",
                next=f"{verb}(kind='plan', id='pe42', …)",
            )
        return str(id)

    def _resolve_at_anchors(self, at: dict[str, Any] | None) -> dict[str, Any] | None:
        """Rewrite any ``pe<id>`` anchor in an ``at=`` intent to the internal
        base-58 handle the store mutator keys on (the store's ``_resolve_at``
        resolves by the ``handle`` column, not the ``pe`` universal handle)."""
        if not at:
            return at
        out = dict(at)
        for key in ("before", "after", "into"):
            anchor = out.get(key)
            if anchor is not None and _is_plan_chunk_addr(str(anchor)):
                node = self.store.get_draft_chunk(str(anchor), kind="plan")
                if node is None:
                    raise NotFound(f"at: no plan node {anchor!r}")
                out[key] = node.handle
        return out

    def _resolve_move_anchors(self, move: dict[str, Any]) -> dict[str, Any]:
        """Same anchor rewrite as :meth:`_resolve_at_anchors`, for ``move=``."""
        resolved = self._resolve_at_anchors(move)
        assert resolved is not None
        return resolved

    def _marker_patch(
        self, status: str | None, belief: str | None, *, clear: bool
    ) -> dict[str, Any]:
        """Validate + build the ``meta`` patch for status/belief markers.

        On ``edit`` (``clear=True``) an empty string clears the marker; on
        ``put`` (``clear=False``) an absent marker is simply omitted."""
        patch: dict[str, Any] = {}
        if status is not None:
            st = str(status).strip()
            if clear and st == "":
                patch["status"] = None
            elif st in _VALID_STATUS:
                patch["status"] = st
            else:
                raise BadInput(
                    f"status must be one of {list(_VALID_STATUS)}",
                    next="status='open' | 'wip' | 'done'",
                )
        if belief is not None:
            bl = str(belief).strip()
            if clear and bl == "":
                patch["belief"] = None
            elif bl in _VALID_BELIEF:
                patch["belief"] = bl
            else:
                raise BadInput(
                    f"belief must be one of {list(_VALID_BELIEF)}",
                    next="belief='?' (uncertain) | '⚠' (caution)",
                )
        return patch

    def _marker(self, chunk: Any, *, is_cursor: bool = False) -> str:
        """The rendered marker for a node: ``▸`` at the cursor, else the
        status marker (default ``[open]``) with a belief prefix when set."""
        if is_cursor:
            return "▸"
        meta = chunk.meta or {}
        status = str(meta.get("status") or "open")
        marker = _STATUS_MARKERS.get(status, _STATUS_MARKERS["open"])
        belief = meta.get("belief")
        if belief in _VALID_BELIEF:
            return f"{belief}{marker}"
        return marker

    def _render_list(self) -> Response:
        return render_slug_ref_list(
            self.store,
            kind="plan",
            label_plural="plan(s)",
            empty_body="no plans yet — put(kind='plan', id='…', project=<todo>)",
        )

    def _render_outline(self, slug: str, ref: Any) -> Response:
        """Whole-tree render with status markers + the ▸ cursor (ADR 0051
        §2b). One line per node: ``{indent}{marker} {handle} {gloss}``.

        The cursor is ``meta.cursor`` on the plan ref; cold-start (unset)
        falls back to the first ``open`` todo node in reading order. The
        plan's root title heading (born with the plan) is its *name*, not a
        todo item — it renders bare (no status marker) and is never the
        cold-start cursor."""
        chunks = self.store.reading_order(ref.id, kind="plan")
        # The title is the very first node in reading order (created at
        # plan-birth with the smallest pos); it is structural, not a todo.
        title_id = chunks[0].chunk_id if chunks else None
        cursor = str((ref.meta or {}).get("cursor") or "").strip()
        if not cursor:
            cursor = next(
                (
                    c.dc
                    for c in chunks
                    if c.chunk_id != title_id
                    and str((c.meta or {}).get("status") or "open") == "open"
                ),
                "",
            )
        views = self.store.block_views(ref.id)
        n = len(chunks)
        lines = [f"# {ref.title}  ({slug}) — {n} node{'' if n == 1 else 's'}\n"]
        for c in chunks:
            if c.chunk_id == title_id:
                marker = "▸" if c.dc == cursor else "#"
            else:
                marker = self._marker(c, is_cursor=(c.dc == cursor))
            v = views.get(c.handle, {})
            gloss = v.get("summary") or v.get("keywords") or c.text or ""
            lines.append(f"{'  ' * c.depth}{marker} {c.dc} {_cap(gloss)}".rstrip())
        return Response(body="\n".join(lines))

    def _render_chunk(self, addr: str) -> Response:
        """One node verbatim + a small relative window (ADR 0036 relative
        nav: ``pe<id>^`` ancestor / ``+N`` step / ``-lo..hi`` span)."""
        rel = handle_registry.parse_relative(addr)
        if rel is not None:
            ids = self.store.draft_relative_chunk_ids(addr, kind="plan")
            if not ids:
                raise NotFound(f"plan node {addr!r} resolves to nothing (out of range)")
            window = [
                node
                for cid in ids
                if (node := self.store.get_draft_chunk(f"pe{cid}", kind="plan"))
                is not None
            ]
        else:
            node = self.store.get_draft_chunk(addr, kind="plan")
            if node is None:
                raise NotFound(f"plan node {addr!r} not found")
            window = [node]
        blocks = [
            f"{self._marker(c)} {c.dc}  sha:{content_sha(c.text)[:12]}\n{c.text}"
            for c in window
        ]
        return Response(body="\n\n".join(blocks))
