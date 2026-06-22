"""DraftHandler — the editable document kind (ADR 0033).

A `draft` is a slug-addressed ref whose body chunks are mutable in
structure (reorder/reparent) and text. The handler wraps the
:class:`~precis.store._draft_ops.DraftMixin` store ops behind the
existing seven verbs — **no new verbs**:

- ``put``   — create a draft (`project=`, born with a title heading) or
  add a chunk (`chunk_kind=`, `text=`, placed by `at=`).
- ``get``   — list drafts (no id), a draft's outline (`id='<slug>'`), or
  a chunk verbatim with a reading window (`id='¶<handle>[-B][+A]'`).
- ``edit``  — change a chunk's text (`text=`) or move it (`move=`).
- ``delete``— soft-retire a chunk (`mode='cascade'|'promote'` for a
  heading with children).

Chunks are addressed by the opaque ``¶<handle>``; the draft itself by
its slug (the universal ``id=``). See ``precis-draft-help``.
"""

from __future__ import annotations

import logging
import re
from typing import Any, ClassVar

from precis.dispatch import Hub, InitError
from precis.errors import BadInput, NotFound
from precis.format import toon
from precis.handlers._slug_ref_shared import (
    render_slug_ref_list,
    resolve_live_slug_ref,
)
from precis.protocol import Handler, KindSpec
from precis.response import Response
from precis.store._draft_ops import content_sha
from precis.utils.embed_query import query_vec_for

log = logging.getLogger(__name__)

_CHUNK_ADDR = re.compile(r"^¶(?P<h>[A-Za-z0-9]+)(?:-(?P<b>\d+))?(?:\+(?P<a>\d+))?$")


class DraftHandler(Handler):
    spec: ClassVar[KindSpec] = KindSpec(
        kind="draft",
        title="Draft",
        description=(
            "Editable, chunk-native document (ADR 0033). put creates a "
            "draft (project=, born with a title heading) or adds a chunk "
            "(chunk_kind=, text=, at={first|last|into|before|after}); get "
            "lists / outlines / reads a chunk window ¶handle-B+A; search "
            "(q=, mode=lexical|semantic|hybrid, scope=slug|¶handle, "
            "headings_only=) over prose; edit changes text or moves "
            "(move=); delete soft-retires (mode=cascade|promote). Chunks "
            "addressed by ¶handle. See precis-draft-help."
        ),
        supports_get=True,
        supports_search=True,
        supports_put=True,
        supports_edit=True,
        supports_delete=True,
        is_numeric=False,
        id_required=False,
        note_like=True,
        views=("toc",),
    )

    def __init__(self, *, hub: Hub) -> None:
        if hub.store is None:
            raise InitError("draft: store required")
        self.store = hub.store
        self.embedder = hub.embedder

    # ── get ──────────────────────────────────────────────────────────

    def get(  # type: ignore[override]
        self, *, id: str | int | None = None, view: str | None = None, **_kw: Any
    ) -> Response:
        if id is None or (isinstance(id, str) and id.strip() in ("", "/")):
            return self._render_list()
        s = str(id).strip()
        if s.startswith("¶"):
            if view == "toc":  # TOC of the subtree under this heading
                return self._render_toc(root_handle=s)
            return self._render_chunk(s)
        ref = resolve_live_slug_ref(self.store, kind="draft", id=s)
        if view == "toc":
            return self._render_toc(ref=ref)
        if view is not None:
            raise BadInput(
                f"unknown draft view {view!r}",
                next="view='toc' for the heading skeleton, or omit for the outline",
            )
        return self._render_outline(s, ref)

    # ── search: lexical / semantic over draft chunks ─────────────────

    def search(  # type: ignore[override]
        self,
        *,
        q: str | None = None,
        scope: str | int | None = None,
        id: str | int | None = None,
        mode: str | None = None,
        headings_only: bool = False,
        page_size: int = 10,
        page: int = 1,
        **_kw: Any,
    ) -> Response:
        """Search draft prose. ``mode='lexical'`` is verbatim/keyword,
        ``mode='semantic'`` is by meaning, default ``hybrid`` fuses both.
        Scope: a ``¶handle`` searches the subtree under that chunk, a
        draft slug searches that whole draft, nothing searches every
        draft. ``headings_only=True`` restricts hits to section headings
        (a semantic TOC jump)."""
        if q is None or not str(q).strip():
            raise BadInput(
                "search(kind='draft') requires q=",
                next="search(kind='draft', q='topic', mode='semantic')",
            )
        q = str(q)
        # ``id='¶…'`` is accepted as a scope alias — the sigil already
        # pinned kind='draft', and an agent naturally points search at the
        # chunk it is reading.
        raw_scope = next(
            (str(c).strip() for c in (scope, id) if c is not None and str(c).strip()),
            None,
        )
        scope_ref_id: int | None = None
        chunk_ids: list[int] | None = None
        where = "all drafts"
        if raw_scope:
            if raw_scope.startswith("¶"):
                chunk_ids = self.store.draft_subtree_chunk_ids(raw_scope)
                if not chunk_ids:
                    raise NotFound(f"draft chunk {raw_scope} not found")
                root = self.store.get_draft_chunk(raw_scope)
                scope_ref_id = int(root.ref_id) if root else None
                where = f"subtree {raw_scope}"
            else:
                ref = resolve_live_slug_ref(self.store, kind="draft", id=raw_scope)
                scope_ref_id = ref.id
                where = f"draft {raw_scope!r}"
        chunk_kinds = ["heading"] if headings_only else None
        query_vec = query_vec_for(self.embedder, q, mode)
        offset = max(0, (int(page) - 1) * int(page_size))
        hits = self.store.search_blocks(
            q=q,
            query_vec=query_vec,
            mode=mode,
            kind="draft",
            scope_ref_id=scope_ref_id,
            chunk_ids=chunk_ids,
            chunk_kinds=chunk_kinds,
            limit=page_size,
            offset=offset,
        )
        return self._render_search(
            hits, q=q, where=where, headings_only=headings_only
        )

    def _render_search(
        self, hits: list[Any], *, q: str, where: str, headings_only: bool
    ) -> Response:
        noun = "heading" if headings_only else "chunk"
        if not hits:
            return Response(
                body=(
                    f"no draft {noun}s match {q!r} in {where}\n\n"
                    "Next: widen with mode='semantic', drop scope=, or "
                    "drop headings_only to search body text too."
                )
            )
        handles = self.store.draft_handles_for([b.id for b, _r, _s in hits])
        lines = [f"# {len(hits)} draft {noun} hit(s) for {q!r} — {where}\n"]
        for block, ref, _score in hits:
            handle = handles.get(block.id, "?")
            draft = ref.slug or ref.id
            first = (block.text or "").strip().splitlines()[0] if block.text else ""
            if len(first) > 90:
                first = first[:89] + "…"
            lines.append(f"draft:{draft}  ¶{handle}  [{block.chunk_kind}] {first}")
        lines.append("\nNext: get(id='¶<handle>') to read any hit in full.")
        return Response(body="\n".join(lines))

    # ── put: create a draft, or add a chunk ──────────────────────────

    def put(  # type: ignore[override]
        self,
        *,
        id: str | int | None = None,
        text: str | None = None,
        title: str | None = None,
        project: str | int | None = None,
        chunk_kind: str | None = None,
        at: dict[str, Any] | None = None,
        meta: dict[str, Any] | None = None,
        **_kw: Any,
    ) -> Response:
        if id is None or not str(id).strip():
            raise BadInput(
                "put(kind='draft') requires id= (the draft slug)",
                next="put(kind='draft', id='nanotrans', title='…', project=<todo-id>)",
            )
        slug = str(id).strip()

        if chunk_kind is not None or at is not None:
            ref = resolve_live_slug_ref(self.store, kind="draft", id=slug)
            if text is None or not str(text).strip():
                raise BadInput(
                    "adding a draft chunk requires text=",
                    next="put(kind='draft', id='nanotrans', chunk_kind='paragraph', text='…', at={'after': '¶<handle>'})",
                )
            kind = chunk_kind or "paragraph"
            # A glossary ``term`` files under an auto-created "Glossary"
            # heading (the doc's glossary subtree) unless the caller placed
            # it explicitly.
            if kind == "term" and at is None:
                at = {"into": "¶" + self.store.ensure_glossary_heading(ref.id)}
            chunks = self.store.add_chunks(
                ref_id=ref.id,
                chunk_kind=kind,
                text=str(text),
                at=at,
                meta=meta,
            )
            self._sync_draft_links(ref.id)
            handles = " ".join(f"¶{c.handle}" for c in chunks)
            n = len(chunks)
            body = f"added {n} chunk{'' if n == 1 else 's'} to {slug}: {handles}"
            # Hint the LLM about any undefined abbreviations it just wrote
            # (skip when the write *is* a term definition).
            if kind != "term":
                undefined = self.store.undefined_abbrevs(ref.id, str(text))
                body += self._abbrev_hint(slug, undefined)
                body += self._citation_form_hint(str(text))
            return Response(body=body)

        # else: create the draft
        if project is None:
            raise BadInput(
                "creating a draft requires project= (the owning project todo id)",
                next="put(kind='draft', id='nanotrans', title='…', project=<todo-id>)",
            )
        project_ref_id = self._resolve_project(project)
        ref, title_chunk = self.store.create_draft(
            name=slug,
            title=(title or slug).strip() or slug,
            project_ref_id=project_ref_id,
            meta=meta,
        )
        return Response(
            body=(
                f"created draft '{slug}' (title heading ¶{title_chunk.handle}); "
                f"linked draft-of project {project_ref_id}"
            )
        )

    # ── edit: text or move ───────────────────────────────────────────

    def edit(  # type: ignore[override]
        self,
        *,
        id: str | int | None = None,
        text: str | None = None,
        move: dict[str, Any] | None = None,
        base_sha: str | None = None,
        not_abbrev: list[str] | str | None = None,
        **_kw: Any,
    ) -> Response:
        # ``not_abbrev`` is a draft-level op (silence the undefined-abbrev
        # hint) — id may be the slug or any ¶handle in the draft.
        if not_abbrev:
            tokens = [not_abbrev] if isinstance(not_abbrev, str) else list(not_abbrev)
            ref = self._resolve_draft_any(id)
            self.store.add_abbrev_ignore(ref.id, tokens)
            return Response(body=f"marked not-an-abbrev: {', '.join(tokens)}")
        handle = self._require_chunk_id(id, verb="edit")
        if move is not None:
            c = self.store.move_chunk(handle, move)
            return Response(body=f"moved ¶{c.handle}")
        if text is not None:
            c = self.store.edit_text(handle, str(text), base_sha=base_sha)
            body = f"edited ¶{c.handle}" if c else "edited"
            if c is not None:
                self._sync_draft_links(c.ref_id)
                ref = self.store.get_ref(kind="draft", id=int(c.ref_id))
                slug = ref.slug if ref and ref.slug else str(c.ref_id)
                body += self._abbrev_hint(
                    slug, self.store.undefined_abbrevs(c.ref_id, str(text))
                )
                body += self._citation_form_hint(str(text))
            return Response(body=body)
        raise BadInput(
            "edit(kind='draft') requires text= (rewrite), move= (reorder/reparent), "
            "or not_abbrev= (silence the abbrev hint)",
            next="edit(kind='draft', id='¶<handle>', text='…')",
        )

    # ── delete: soft-retire ──────────────────────────────────────────

    def delete(  # type: ignore[override]
        self,
        *,
        id: str | int | None = None,
        mode: str | None = None,
        **_kw: Any,
    ) -> Response:
        handle = self._require_chunk_id(id, verb="delete")
        chunk = self.store.get_draft_chunk(str(handle).lstrip("¶"))
        self.store.retire_chunk(handle, mode=mode)
        if chunk is not None:
            self._sync_draft_links(chunk.ref_id)
        return Response(body=f"retired ¶{handle}")

    # ── helpers ──────────────────────────────────────────────────────

    def _abbrev_hint(self, slug: str, undefined: list[str]) -> str:
        """A hint (appended to the write/edit Response) listing undefined
        abbreviations with copy-ready calls to define or silence them."""
        if not undefined:
            return ""
        toks = ", ".join(undefined)
        first = undefined[0]
        return (
            f"\n\n⚠ undefined abbreviation(s): {toks}. For each, either DEFINE it — "
            f"put(kind='draft', id={slug!r}, chunk_kind='term', text='<expansion>', "
            f"meta={{'short': {first!r}}}) — or, if it isn't an abbreviation, SILENCE "
            f"it: edit(kind='draft', id={slug!r}, not_abbrev=[{first!r}])."
        )

    def _citation_form_hint(self, text: str) -> str:
        """Nudge toward the canonical ``[§<cite_key>~<n>]`` citation when
        the text cites a paper by the bare ``paper:<id>`` mention —
        especially a numeric ref id, which resolves but is opaque,
        unstable across re-ingest, and exports to no ``\\cite``. Only the
        prefixed ``paper:`` form fires; the ``§`` bracket and bare
        cite_key forms (the acceptable ones) are left alone."""
        from precis.utils import mentions

        suggestions: dict[str, str] = {}
        for m in mentions.REF_PATTERN.finditer(text):
            if m.group("kind") != "paper":
                continue
            ident = m.group("id").lstrip("#")
            suffix = m.group("chunk") or ""
            ref = mentions.resolve_handle_ref(self.store, ident)
            cite_key = getattr(ref, "slug", None) if ref is not None else None
            if not cite_key:
                continue
            suggestions[f"paper:{ident}{suffix}"] = f"[§{cite_key}{suffix}]"
        if not suggestions:
            return ""
        pairs = "; ".join(f"{o} → {s}" for o, s in list(suggestions.items())[:5])
        return (
            "\n\n⚠ cite papers as [§<cite_key>~<chunk>], not the bare paper: "
            f"mention (a numeric ref id exports to no \\cite): {pairs}."
        )

    def _resolve_draft_any(self, id: str | int | None) -> Any:
        """Resolve a draft ref from either its slug or a ¶handle (a chunk
        in it). Used by the draft-level ``not_abbrev`` op."""
        s = str(id or "").strip()
        if s.startswith("¶"):
            chunk = self.store.get_draft_chunk(s.lstrip("¶"))
            if chunk is None:
                raise NotFound(f"draft chunk {s} not found")
            ref = self.store.get_ref(kind="draft", id=int(chunk.ref_id))
            if ref is None:
                raise NotFound(f"draft for chunk {s} not found")
            return ref
        return resolve_live_slug_ref(self.store, kind="draft", id=s)

    def _require_chunk_id(self, id: str | int | None, *, verb: str) -> str:
        if id is None or not str(id).startswith("¶"):
            raise BadInput(
                f"{verb}(kind='draft') targets a chunk — id='¶<handle>'",
                next=f"{verb}(kind='draft', id='¶5BL5xQ', …)",
            )
        return str(id)

    def _sync_draft_links(self, ref_id: int) -> None:
        """Materialise ``related-to`` links from this draft to every ref
        its chunks reference — the superset grammar (``kind:ref`` mentions,
        ``¶`` cross-refs, ``§`` citations). Recomputed over the *whole*
        draft on each write (chunk edits add/remove references), replacing
        the prior ``auto='mention'`` set so a removed reference loses its
        link. Best-effort: a resolution failure never fails the write —
        mirrors the note autolinker (`_numeric_ref._sync_mention_links`).
        """
        from precis.utils import draft_markup

        try:
            chunks = self.store.reading_order(ref_id)
            text = "\n\n".join(c.text for c in chunks)
            targets = draft_markup.resolve_draft_link_targets(
                self.store, text, exclude_ref_id=ref_id
            )
            wanted = {(t.dst_ref_id, t.dst_pos) for t in targets}
            for link in self.store.links_for(
                ref_id, direction="out", relation="related-to"
            ):
                if (link.meta or {}).get("auto") == "mention" and (
                    link.dst_ref_id,
                    link.dst_pos,
                ) not in wanted:
                    self.store.remove_link(
                        src_ref_id=ref_id,
                        dst_ref_id=link.dst_ref_id,
                        dst_pos=link.dst_pos,
                        relation="related-to",
                    )
            for t in targets:
                self.store.add_link(
                    src_ref_id=ref_id,
                    dst_ref_id=t.dst_ref_id,
                    dst_pos=t.dst_pos,
                    relation="related-to",
                    set_by="agent",
                    meta={"auto": "mention"},
                )
        except Exception:
            log.warning(
                "draft: autolink mentions failed for ref %s", ref_id, exc_info=True
            )

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

    def _render_list(self) -> Response:
        return render_slug_ref_list(
            self.store,
            kind="draft",
            label_plural="draft(s)",
            empty_body="no drafts yet — put(kind='draft', id='…', project=<todo>)",
        )

    def _render_outline(self, slug: str, ref: Any) -> Response:
        chunks = self.store.reading_order(ref.id)
        # Per-block gloss preference: the llm-v1 summary, else the keyword
        # set, else the truncated first line. Lets the outline read as
        # *meaning* once the summarize/keyword workers have run, degrading
        # to the raw-text peek for blocks they haven't reached yet.
        views = self.store.block_views(ref.id)
        n = len(chunks)
        lines = [f"# {ref.title}  ({slug}) — {n} chunk{'' if n == 1 else 's'}\n"]
        for c in chunks:
            v = views.get(c.handle, {})
            gloss = v.get("summary") or v.get("keywords") or ""
            if not gloss:
                gloss = c.text.splitlines()[0] if c.text else ""
            # collapse to a single line; cap so the outline stays scannable
            gloss = " ".join(gloss.split())
            if len(gloss) > 200:
                gloss = gloss[:199] + "…"
            lines.append(f"{'  ' * c.depth}¶{c.handle}  [{c.chunk_kind}] {gloss}")
        return Response(body="\n".join(lines))

    def _render_chunk(self, addr: str) -> Response:
        m = _CHUNK_ADDR.match(addr)
        if m is None:
            raise BadInput(
                f"unparseable chunk address {addr!r}",
                next="id='¶<handle>' or '¶<handle>-5+3' for a window",
            )
        handle = m.group("h")
        before = int(m.group("b") or 0)
        after = int(m.group("a") or 0)
        chunk = self.store.get_draft_chunk(handle)
        if chunk is None:
            raise NotFound(f"draft chunk ¶{handle} not found")
        order = self.store.reading_order(chunk.ref_id)
        idx = next((i for i, c in enumerate(order) if c.handle == handle), None)
        if idx is None:  # retired — show it alone
            window = [chunk]
        else:
            window = order[max(0, idx - before) : idx + after + 1]
        # ``sha:`` is a short prefix of the chunk's content_sha — pass it
        # back as ``edit(base_sha=…)`` for an optimistic edit that won't
        # clobber a change that landed since this read. 12 hex chars (48
        # bits) is ample to detect a change to one chunk; the full digest
        # is needlessly long on every line. ``edit`` matches by prefix, so
        # a full 64-char sha still works.
        blocks = [
            f"¶{c.handle}  [{c.chunk_kind}]  sha:{content_sha(c.text)[:12]}\n{c.text}"
            for c in window
        ]
        return Response(body="\n\n".join(blocks))

    def _render_toc(
        self, *, ref: Any = None, root_handle: str | None = None
    ) -> Response:
        """The heading skeleton — whole draft, or the subtree under a
        heading (`view='toc'` at any hierarchy level). Computed §-numbers,
        with each heading's gist/keywords when a worker has produced them."""
        if root_handle is not None:
            chunk = self.store.get_draft_chunk(root_handle)
            if chunk is None:
                raise NotFound(f"draft heading {root_handle} not found")
            entries = self.store.draft_toc(chunk.ref_id, root_handle=root_handle)
            header = f"# TOC under ¶{chunk.handle}: {chunk.text}"
        else:
            entries = self.store.draft_toc(ref.id)
            header = f"# {ref.title} — table of contents"
        if not entries:
            return Response(body=f"{header}\n\n(no sub-headings yet)")
        # TOON table (ADR 0002 — the house format for tabular tool output).
        # `level` (tree depth) conveys hierarchy since TOON is flat; the
        # stable `¶handle` is the address the agent navigates/edits by.
        # Display §-numbers are positional (computed at render/export, not
        # here — they'd rot on reorder and aren't a valid handle).
        rows = [
            {
                "handle": f"¶{e.handle}",
                "level": e.depth,
                "title": e.title,
                "gist": e.gist or (", ".join(e.keywords[:6]) if e.keywords else ""),
            }
            for e in entries
        ]
        table = toon.dump(rows, schema=["handle", "level", "title", "gist"])
        return Response(body=f"{header}\n\n{table}")
