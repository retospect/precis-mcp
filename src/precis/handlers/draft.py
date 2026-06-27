"""DraftHandler — the editable document kind (ADR 0033).

A `draft` is a slug-addressed ref whose body chunks are mutable in
structure (reorder/reparent) and text. The handler wraps the
:class:`~precis.store._draft_ops.DraftMixin` store ops behind the
existing seven verbs — **no new verbs**:

- ``put``   — create a draft (`project=`, born with a title heading) or
  add a chunk (`chunk_kind=`, `text=`, placed by `at=`).
- ``get``   — list drafts (no id), a draft's outline (`id='<slug>'`), or
  a chunk verbatim with a relative window (`id='dc<id>'`, `dc<id>-2..3`).
- ``edit``  — change a chunk's text (`text=`) or move it (`move=`).
- ``delete``— soft-retire a chunk (`mode='cascade'|'promote'` for a
  heading with children).

Chunks are addressed by the computed ``dc<chunk_id>`` handle (ADR 0036;
the legacy ``¶<base58>`` still resolves during the transition); the draft
itself by its slug (the universal ``id=``). See ``precis-draft-help``.
"""

from __future__ import annotations

import base64
import binascii
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
from precis.utils import handle_registry
from precis.utils.embed_query import query_vec_for
from precis.utils.table_data import normalize_table, table_to_markdown

log = logging.getLogger(__name__)

# A bare draft chunk address: the ADR 0036 universal handle ``dc<chunk_id>``
# or the legacy ADR-0033 ``¶<base58>``. Relative navigation (``^`` / ``+N`` /
# ``-lo..hi``) is parsed separately via ``handle_registry.parse_relative``.
_CHUNK_ADDR = re.compile(r"^(?:dc(?P<cid>\d+)|¶(?P<h>[A-Za-z0-9]+))$")

#: Recognises a draft chunk address — bare or with an ADR 0036 relative
#: operator (``^``/``+``/``-``/``..``) — used to tell a chunk address from a
#: draft slug in ``get`` / ``search``.
_DRAFT_CHUNK_ADDR_RE = re.compile(r"^(?:dc\d+|¶[A-Za-z0-9]+)(?:[+\-^].*|\.\..*)?$")


def _is_draft_chunk_addr(s: str) -> bool:
    """True iff ``s`` addresses a draft chunk (``dc<id>`` / ``¶<base58>``,
    optionally with a relative operator)."""
    return bool(_DRAFT_CHUNK_ADDR_RE.match(s.strip()))


#: A figure's origin class (ADR 0034) — drives the clearance gate. ``original``
#: is ours; ``own_graph`` is generated from data (ships a data supplement);
#: ``third_party`` is reused under a publisher permission (carries the paper-trail).
_FIGURE_ORIGINS = ("original", "own_graph", "third_party")

#: magic-byte → mime sniff for a pasted image when ``mime=`` is omitted.
_MAGIC_MIME: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


#: A 1×1 transparent PNG, the deferred-image placeholder for a computed graph
#: figure (ADR 0035) — there's always a `chunk_blobs` row so the reader/export
#: never hit a missing blob; the render pass overwrites it with the real chart.
_PLACEHOLDER_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


def _sniff_mime(raw: bytes) -> str:
    """Best-effort image mime from magic bytes; WEBP needs the RIFF check."""
    for sig, mime in _MAGIC_MIME:
        if raw.startswith(sig):
            return mime
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return "application/octet-stream"


class DraftHandler(Handler):
    spec: ClassVar[KindSpec] = KindSpec(
        kind="draft",
        title="Draft",
        description=(
            "Editable, chunk-native document (ADR 0033). put creates a "
            "draft (project=, born with a title heading) or adds a chunk "
            "(chunk_kind=, text=, at={first|last|into|before|after}); get "
            "lists / outlines / reads a chunk window dc<id>-B+A; search "
            "(q=, mode=lexical|semantic|hybrid, scope=slug|dc<id>, "
            "headings_only=) over prose; edit changes text, moves "
            "(move=), or sets a heading's section style (style=<skill>); "
            "delete soft-retires (mode=cascade|promote). Chunks "
            "addressed by dc<chunk_id> (legacy ¶handle still resolves). "
            "See precis-draft-help."
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
        if _is_draft_chunk_addr(s):
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
        Scope: a ``dc<id>`` chunk handle searches the subtree under that
        chunk, a draft slug searches that whole draft, nothing searches
        every draft. ``headings_only=True`` restricts hits to section
        headings (a semantic TOC jump)."""
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
            if _is_draft_chunk_addr(raw_scope):
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
        return self._render_search(hits, q=q, where=where, headings_only=headings_only)

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
        lines = [f"# {len(hits)} draft {noun} hit(s) for {q!r} — {where}\n"]
        for block, ref, _score in hits:
            handle = handle_registry.format_handle("draft", block.id, chunk=True)
            draft = ref.slug or ref.id
            first = (block.text or "").strip().splitlines()[0] if block.text else ""
            if len(first) > 90:
                first = first[:89] + "…"
            lines.append(f"draft:{draft}  {handle}  [{block.chunk_kind}] {first}")
        lines.append("\nNext: get(id='dc<chunk_id>') to read any hit in full.")
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
        image: str | None = None,
        mime: str | None = None,
        origin: str | None = None,
        permission: dict[str, Any] | None = None,
        table: dict[str, Any] | None = None,
        caption: str | None = None,
        regen: dict[str, Any] | None = None,
        render: str | None = None,
        plots: list[str] | None = None,
        **_kw: Any,
    ) -> Response:
        if id is None or not str(id).strip():
            raise BadInput(
                "put(kind='draft') requires id= (the draft slug)",
                next="put(kind='draft', id='nanotrans', title='…', project=<todo-id>)",
            )
        slug = str(id).strip()

        if chunk_kind == "figure" and image is not None:
            ref = resolve_live_slug_ref(self.store, kind="draft", id=slug)
            return self._add_figure(
                slug=slug,
                ref_id=ref.id,
                caption=text,
                image=image,
                mime=mime,
                origin=origin,
                permission=permission,
                at=at,
            )

        # A computed figure (graph): render code + plots links, image deferred
        # to the render pass (ADR 0035). text= is the caption.
        if chunk_kind == "figure" and (render is not None or plots is not None):
            ref = resolve_live_slug_ref(self.store, kind="draft", id=slug)
            return self._add_graph_figure(
                slug=slug,
                ref_id=ref.id,
                caption=text or caption,
                render=render,
                plots=plots,
                at=at,
            )

        if chunk_kind is not None or at is not None:
            ref = resolve_live_slug_ref(self.store, kind="draft", id=slug)
            if (chunk_kind or "paragraph") == "table" or table is not None:
                return self._put_table(
                    slug,
                    ref,
                    table=table,
                    caption=caption,
                    regen=regen,
                    at=at,
                    meta=meta,
                )
            if text is None or not str(text).strip():
                raise BadInput(
                    "adding a draft chunk requires text=",
                    next="put(kind='draft', id='nanotrans', chunk_kind='paragraph', text='…', at={'after': 'dc<chunk_id>'})",
                )
            kind = chunk_kind or "paragraph"
            # A glossary ``term`` files under an auto-created "Glossary"
            # heading (the doc's glossary subtree) unless the caller placed
            # it explicitly.
            if kind == "term" and at is None:
                at = {"into": self.store.ensure_glossary_heading(ref.id)}
            chunks = self.store.add_chunks(
                ref_id=ref.id,
                chunk_kind=kind,
                text=str(text),
                at=at,
                meta=meta,
            )
            self._sync_draft_links(ref.id)
            self._attribute_touch([c.chunk_id for c in chunks])
            handles = " ".join(f"{c.dc}" for c in chunks)
            n = len(chunks)
            body = f"added {n} chunk{'' if n == 1 else 's'} to {slug}: {handles}"
            # Hint the LLM about abbreviations it just wrote (skip when the
            # write *is* a term definition). All of a new chunk's text is
            # "newly introduced", so there's no prior text to diff against.
            if kind != "term":
                body += self._write_abbrev_hints(slug, ref.id, str(text), "")
                body += self._citation_form_hint(str(text))
                body += self._literal_cite_hint(str(text))
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
                f"created draft '{slug}' (title heading {title_chunk.dc}); "
                f"linked draft-of project {project_ref_id}"
            )
        )

    def _add_figure(
        self,
        *,
        slug: str,
        ref_id: int,
        caption: str | None,
        image: str,
        mime: str | None,
        origin: str | None,
        permission: dict[str, Any] | None,
        at: dict[str, Any] | None,
    ) -> Response:
        """Add a figure chunk with binary payload (ADR 0034). ``text`` is
        the caption; ``image`` is base64 bytes; ``origin`` classes the
        figure for the clearance gate; a ``third_party`` figure must carry
        a ``permission`` paper-trail."""
        if caption is None or not str(caption).strip():
            raise BadInput(
                "a figure requires text= (the caption)",
                next="put(kind='draft', id='…', chunk_kind='figure', text='Fig 1. …', image=<b64>, origin='original')",
            )
        org = (origin or "").strip()
        if org not in _FIGURE_ORIGINS:
            raise BadInput(
                f"figure origin= must be one of {list(_FIGURE_ORIGINS)}",
                next="origin='original' (ours) | 'own_graph' (from data) | 'third_party' (publisher permission)",
            )
        try:
            raw = base64.b64decode(str(image), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise BadInput(
                "image= must be base64-encoded image bytes",
                next="pass the raw image base64-encoded (no data: URI prefix)",
            ) from exc
        if not raw:
            raise BadInput("image= decoded to empty bytes")
        fig_meta: dict[str, Any] = {}
        if org == "third_party":
            if not permission:
                raise BadInput(
                    "a third_party figure requires permission= (the publisher paper-trail)",
                    next=(
                        "permission={'publisher':'…','permission_id':'…',"
                        "'status':'granted','source_paper':'<cite-key>', …}"
                    ),
                )
            fig_meta["permission"] = permission
        chunk = self.store.add_figure(
            ref_id=ref_id,
            caption=str(caption),
            origin=org,
            image=raw,
            mime=(mime or _sniff_mime(raw)),
            at=at,
            figure_meta=fig_meta,
        )
        self._sync_draft_links(ref_id)
        return Response(
            body=f"added figure {chunk.dc} [{org}] to {slug} ({len(raw)} bytes)"
        )

    def _add_graph_figure(
        self,
        *,
        slug: str,
        ref_id: int,
        caption: str | None,
        render: str | None,
        plots: list[str] | None,
        at: dict[str, Any] | None,
    ) -> Response:
        """Add a *computed* figure — a graph (ADR 0035): the render code goes to
        ``meta.render``, ``plots`` links the data chunks it renders, and the image
        is **deferred** (a placeholder blob until the render pass fills it).
        ``origin='own_graph'``; the caption is the face (``text``)."""
        if not render or not str(render).strip():
            raise BadInput(
                "a graph figure requires render= (the Python that draws it)",
                next=(
                    "put(kind='draft', id='…', chunk_kind='figure', "
                    "render='import matplotlib.pyplot as plt; …', "
                    "plots=['dc<data-id>'], text='Fig 1. …')"
                ),
            )
        if not plots:
            raise BadInput(
                "a graph figure requires plots=[dc<id>] — the data chunk(s) it renders",
                next="plots=['dc<table-chunk-id>']  (the table/data chunk handles)",
            )
        if caption is None or not str(caption).strip():
            raise BadInput(
                "a figure requires text= (the caption)",
                next="put(kind='draft', …, chunk_kind='figure', render=…, plots=[…], text='Fig 1. …')",
            )
        # Resolve each plots target to a live chunk in *this* draft.
        targets = []
        for p in plots:
            c = self.store.get_draft_chunk(str(p))
            if c is None:
                raise NotFound(f"plots target {p!r} not found")
            if int(c.ref_id) != ref_id:
                raise BadInput(
                    f"plots target {p!r} is not a chunk in draft {slug!r}",
                    next="plots= must reference data chunks in the same draft",
                )
            targets.append(c)

        chunk = self.store.add_figure(
            ref_id=ref_id,
            caption=str(caption),
            origin="own_graph",
            image=_PLACEHOLDER_PNG,  # deferred — the render pass overwrites it
            mime="image/png",
            at=at,
            figure_meta={"render_pending": True},
        )
        self.store.set_render_recipe(
            chunk.chunk_id,
            {"kind": "code", "lang": "python", "src": str(render)},
        )
        n = self.store.link_figure_plots(chunk.chunk_id, [t.chunk_id for t in targets])
        self._sync_draft_links(ref_id)
        self._attribute_touch([chunk.chunk_id])
        return Response(
            body=(
                f"added graph figure {chunk.dc} to {slug} "
                f"(plots {n} data source{'' if n == 1 else 's'}); "
                "image renders out-of-band — render pending"
            )
        )

    # ── edit: text or move ───────────────────────────────────────────

    def edit(  # type: ignore[override]
        self,
        *,
        id: str | int | None = None,
        text: str | None = None,
        move: dict[str, Any] | None = None,
        style: str | None = None,
        base_sha: str | None = None,
        not_abbrev: list[str] | str | None = None,
        permission: dict[str, Any] | None = None,
        origin: str | None = None,
        table: dict[str, Any] | None = None,
        caption: str | None = None,
        regen: dict[str, Any] | None = None,
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
        # Normalize a ``dc<id>`` address to the legacy base-58 anchor the
        # store mutators still key on; the agent-facing emit uses ``.dc``.
        _base = self.store.get_draft_chunk(handle)
        if _base is None:
            raise NotFound(f"draft chunk {handle!r} not found")
        handle = _base.handle
        if permission is not None or origin is not None:
            # Edit a figure's provenance (ADR 0034) — caption/bytes untouched.
            if origin is not None and origin not in _FIGURE_ORIGINS:
                raise BadInput(
                    f"figure origin= must be one of {list(_FIGURE_ORIGINS)}",
                    next="origin='original' | 'own_graph' | 'third_party'",
                )
            c = self.store.set_figure_provenance(
                handle, permission=permission, origin=origin
            )
            return Response(body=f"updated figure provenance {c.dc}")
        if style is not None:
            # Set/clear the heading's section style (ADR 0037). Metadata-only
            # (meta.style = a skill slug) — no re-embed.
            c = self.store.set_chunk_style(handle, style or None)
            if style:
                return Response(body=f"styled {c.dc} → {style}")
            return Response(body=f"cleared style on {c.dc}")
        if move is not None:
            c = self.store.move_chunk(handle, move)
            if c is not None:
                self._attribute_touch([c.chunk_id])
            return Response(body=f"moved {c.dc}")
        is_table = _base.chunk_kind == "table"
        if is_table or table is not None or regen is not None:
            return self._edit_table(
                handle,
                _base,
                table=table,
                caption=caption,
                regen=regen,
                base_sha=base_sha,
            )
        if text is not None:
            # Capture the prior text *before* the rewrite so the abbrev
            # hints fire only on what this edit introduced (not on
            # acronyms already living in the chunk — the MOF re-nag).
            prior = self.store.get_draft_chunk(str(handle).lstrip("¶"))
            old_text = prior.text if prior else ""
            c = self.store.edit_text(handle, str(text), base_sha=base_sha)
            body = f"edited {c.dc}" if c else "edited"
            if c is not None:
                self._sync_draft_links(c.ref_id)
                self._attribute_touch([c.chunk_id])
                ref = self.store.get_ref(kind="draft", id=int(c.ref_id))
                slug = ref.slug if ref and ref.slug else str(c.ref_id)
                body += self._write_abbrev_hints(slug, c.ref_id, str(text), old_text)
                body += self._citation_form_hint(str(text))
                body += self._literal_cite_hint(str(text))
            return Response(body=body)
        raise BadInput(
            "edit(kind='draft') requires text= (rewrite), move= (reorder/reparent), "
            "style= (set a heading's section style), or not_abbrev= (silence "
            "the abbrev hint)",
            next="edit(kind='draft', id='dc<chunk_id>', text='…')",
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
        chunk = self.store.get_draft_chunk(handle)
        if chunk is None:
            raise NotFound(f"draft chunk {handle!r} not found")
        self.store.retire_chunk(chunk.handle, mode=mode)
        self._sync_draft_links(chunk.ref_id)
        return Response(body=f"retired {chunk.dc}")

    # ── helpers ──────────────────────────────────────────────────────

    def _write_abbrev_hints(
        self, slug: str, ref_id: int, new_text: str, old_text: str
    ) -> str:
        """Abbreviation feedback for one write, scoped to what it
        *introduced* (so editing a chunk doesn't re-nag about acronyms it
        already contained). Two disjoint hints:

        * **undefined** — acronym-shaped tokens with no definition anywhere
          in the draft (and new in this write): define or silence them.
        * **promote** — an inline ``Long Form (ABBR)`` first-use that works
          but lives only in this chunk's prose and isn't yet a glossary
          ``term``: offer to formalise it (durable across edits). The two
          never overlap — an inline-defined token isn't "undefined".
        """
        from precis.utils.abbreviations import find as _find
        from precis.utils.abbreviations import find_acronyms as _acr

        old_acr = _acr(old_text)
        undefined = [
            a
            for a in self.store.undefined_abbrevs(ref_id, new_text)
            if a not in old_acr
        ]
        old_pairs = _find(old_text)
        terms = self.store.draft_term_shorts(ref_id)
        promote = {
            short: long
            for short, long in _find(new_text).items()
            if short not in old_pairs and short not in terms
        }
        return self._abbrev_hint(slug, undefined) + self._promote_hint(slug, promote)

    def _promote_hint(self, slug: str, promote: dict[str, str]) -> str:
        """Offer to promote inline ``Long Form (ABBR)`` definitions to
        glossary ``term`` chunks — a hint, never a refusal (an inline
        first-use is correct, conventional writing; it's just fragile,
        since it lives in one chunk's prose)."""
        if not promote:
            return ""
        toks = ", ".join(promote)
        short, long = next(iter(promote.items()))
        return (
            f"\n\nℹ inline definition(s): {toks}. They work, but live only in "
            f"this chunk's prose — promote to the glossary so they survive edits: "
            f"put(kind='draft', id={slug!r}, chunk_kind='term', text={long!r}, "
            f"meta={{'short': {short!r}}})."
        )

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
        """Nudge toward the canonical ``[pc<id>]`` paper-chunk citation
        when the text cites a paper by a bare ``paper:<id>`` mention —
        which resolves but is opaque, points at no specific passage, and
        exports to no ``\\cite``. The ``[pc<id>]`` handle (copied from
        ``search``/``get`` output) cites the exact supporting chunk, and
        the export engine renders the bibliography from it. Only the
        prefixed ``paper:`` form fires; bare ``[pc<id>]`` handles (the
        canonical form) are left alone."""
        from precis.utils import mentions

        seen: list[str] = []
        for m in mentions.REF_PATTERN.finditer(text):
            if m.group("kind") != "paper":
                continue
            ident = m.group("id").lstrip("#")
            suffix = m.group("chunk") or ""
            mention = f"paper:{ident}{suffix}"
            if mention not in seen:
                seen.append(mention)
        if not seen:
            return ""
        offenders = ", ".join(seen[:5])
        return (
            "\n\n⚠ cite the supporting paper *chunk* by its handle [pc<id>] "
            "(copy it from search/get output), not a bare paper: mention "
            f"(which exports to no \\cite): {offenders}."
        )

    def _literal_cite_hint(self, text: str) -> str:
        r"""Flag a literal ``\cite{...}`` / ``\citequote{...}`` typed into a
        draft body. In a draft you cite by writing the supporting paper-
        chunk handle inline (``[pc<id>]``); the export engine emits the
        ``\cite`` + bibliography, so a hand-written cite key resolves to
        nothing. Fires only on draft chunks — a real ``.tex`` *file* keeps
        its literal ``\cite`` as source (see precis-tex-help)."""
        import re

        if re.search(r"\\cite(?:quote|p|t|alp|author|year)?\s*\{", text):
            return (
                "\n\n⚠ you typed a literal \\cite/\\citequote in the draft. "
                "Cite by the supporting paper-chunk handle inline instead: "
                "[pc<id>] (copy it from search/get output). The export engine "
                "writes the \\cite and the bibliography; \\cite/\\citequote "
                "are export-only output, never authored in a draft."
            )
        return ""

    def _resolve_draft_any(self, id: str | int | None) -> Any:
        """Resolve a draft ref from either its slug or a ¶handle (a chunk
        in it). Used by the draft-level ``not_abbrev`` op."""
        s = str(id or "").strip()
        if _is_draft_chunk_addr(s):
            chunk = self.store.get_draft_chunk(s)
            if chunk is None:
                raise NotFound(f"draft chunk {s} not found")
            ref = self.store.get_ref(kind="draft", id=int(chunk.ref_id))
            if ref is None:
                raise NotFound(f"draft for chunk {s} not found")
            return ref
        return resolve_live_slug_ref(self.store, kind="draft", id=s)

    def _require_chunk_id(self, id: str | int | None, *, verb: str) -> str:
        if id is None or not _is_draft_chunk_addr(str(id)):
            raise BadInput(
                f"{verb}(kind='draft') targets a chunk — id='dc<chunk_id>'",
                next=f"{verb}(kind='draft', id='dc42', …)",
            )
        return str(id)

    #: Kinds whose chunks are citable literature: a reference to one is a
    #: ``cites`` edge (the bibliography / "who cites this?" graph), not a
    #: ``related-to`` provenance edge. Citations are to the literature;
    #: links (a memory, another draft) are to our own notes.
    _CITABLE_KINDS: ClassVar[frozenset[str]] = frozenset(
        {"paper", "patent", "finding"}
    )

    def _sync_draft_links(self, ref_id: int) -> None:
        """Materialise graph edges from this draft to every ref its chunks
        reference — the superset grammar (``kind:ref`` mentions, ``¶``
        cross-refs, ``§``/``[pc<id>]`` citations). A reference to a
        **citable source** (paper/patent/finding) becomes a ``cites``
        edge; every other reference (a memory, another draft) is a
        ``related-to`` provenance edge. Recomputed over the *whole* draft
        on each write, replacing the prior ``auto='mention'`` set in BOTH
        relations so a removed reference loses its edge. Best-effort: a
        resolution failure never fails the write — mirrors the note
        autolinker (`_numeric_ref._sync_mention_links`).
        """
        from precis.utils import draft_markup

        try:
            chunks = self.store.reading_order(ref_id)
            text = "\n\n".join(c.text for c in chunks)
            targets = draft_markup.resolve_draft_link_targets(
                self.store, text, exclude_ref_id=ref_id
            )
            refs_by_id = self.store.fetch_refs_by_ids(
                [t.dst_ref_id for t in targets]
            )
            # (dst_ref_id, dst_pos) → desired relation, routed by kind.
            wanted: dict[tuple[int, int | None], str] = {}
            for t in targets:
                tref = refs_by_id.get(t.dst_ref_id)
                rel = (
                    "cites"
                    if tref is not None and tref.kind in self._CITABLE_KINDS
                    else "related-to"
                )
                wanted[(t.dst_ref_id, t.dst_pos)] = rel
            # Drop stale auto-mention edges in BOTH relations (a removed
            # reference, or one whose routed relation changed).
            for relation in ("cites", "related-to"):
                for link in self.store.links_for(
                    ref_id, direction="out", relation=relation
                ):
                    if (link.meta or {}).get("auto") != "mention":
                        continue
                    if wanted.get((link.dst_ref_id, link.dst_pos)) != relation:
                        self.store.remove_link(
                            src_ref_id=ref_id,
                            dst_ref_id=link.dst_ref_id,
                            dst_pos=link.dst_pos,
                            relation=relation,
                        )
            for (dst, pos), relation in wanted.items():
                self.store.add_link(
                    src_ref_id=ref_id,
                    dst_ref_id=dst,
                    dst_pos=pos,
                    relation=relation,
                    set_by="agent",
                    meta={"auto": "mention"},
                )
        except Exception:
            log.warning(
                "draft: autolink mentions failed for ref %s", ref_id, exc_info=True
            )

    def _attribute_touch(self, chunk_ids: list[int]) -> None:
        """Attribute the just-written chunks to the current agent run.

        A no-op unless ``PRECIS_CURRENT_AGENTLOG`` is set (the runner
        threads it onto the ``claude -p`` subprocess); an operator console
        edit or a test that didn't open a log just skips attribution.
        Best-effort — never fails the write."""
        from precis import agentlog

        agentlog.touch_from_env(self.store, chunk_ids=chunk_ids)

    # ── data/table chunks (ADR 0035 §1) ──────────────────────────────

    def _put_table(
        self,
        slug: str,
        ref: Any,
        *,
        table: dict[str, Any] | None,
        caption: str | None,
        regen: dict[str, Any] | None,
        at: dict[str, Any] | None,
        meta: dict[str, Any] | None,
    ) -> Response:
        """Add a ``chunk_kind='table'`` data chunk: canonical ``meta.table``
        + derived markdown ``text``. ``meta.regen`` (provenance/how-to-rebuild)
        and ``meta.caption`` (legend) are stamped verbatim — both inert, no
        execution (ADR 0035 §1)."""
        if table is None:
            raise BadInput(
                "a table chunk requires table={header, rows}",
                next=(
                    f"put(kind='draft', id={slug!r}, chunk_kind='table', "
                    "table={'header': ['x','y'], 'rows': [[1,2],[3,4]]}, "
                    "caption='…', at={'last': True})"
                ),
            )
        norm = normalize_table(table)
        cap = caption.strip() if caption and caption.strip() else None
        md = table_to_markdown(norm, caption=cap)
        chunk_meta = dict(meta or {})
        chunk_meta["table"] = norm
        if cap is not None:
            chunk_meta["caption"] = cap
        if regen is not None:
            chunk_meta["regen"] = regen
        chunks = self.store.add_chunks(
            ref_id=ref.id,
            chunk_kind="table",
            text=md,
            at=at,
            meta=chunk_meta,
            split=False,
        )
        self._sync_draft_links(ref.id)
        self._attribute_touch([c.chunk_id for c in chunks])
        c = chunks[0]
        rows, cols = len(norm["rows"]), len(norm["header"])
        return Response(
            body=(
                f"added table {c.dc} to {slug} ({rows} row"
                f"{'' if rows == 1 else 's'} × {cols} col"
                f"{'' if cols == 1 else 's'}); text is the derived markdown — "
                f"edit table=/caption=/regen=, not text="
            )
        )

    def _edit_table(
        self,
        handle: str,
        chunk: Any,
        *,
        table: dict[str, Any] | None,
        caption: str | None,
        regen: dict[str, Any] | None,
        base_sha: str | None,
    ) -> Response:
        """Re-derive a table chunk's markdown from new canonical data /
        legend / provenance. ``text=`` is rejected — a table's text is
        derived (ADR 0035 §1), never hand-edited."""
        if chunk is None or chunk.chunk_kind != "table":
            raise BadInput(
                "table=/regen= apply only to a chunk_kind='table' chunk",
                next="edit(kind='draft', id='dc<chunk_id>', table={…})",
            )
        if table is None and caption is None and regen is None:
            raise BadInput(
                "a table chunk's text is derived from its data — pass "
                "table={header,rows}, caption=, or regen= (not text=)",
                next="edit(kind='draft', id='dc<chunk_id>', table={'header': […], 'rows': […]})",
            )
        cur = self.store.draft_chunk_meta(handle)
        norm = normalize_table(table) if table is not None else cur.get("table")
        if not norm:
            raise BadInput(
                "this table chunk has no stored data — pass table={header, rows}",
                next="edit(kind='draft', id='dc<chunk_id>', table={'header': […], 'rows': […]})",
            )
        cap = caption.strip() if caption else cur.get("caption")
        cap = cap or None
        md = table_to_markdown(norm, caption=cap)
        patch: dict[str, Any] = {"table": norm}
        if caption is not None:
            patch["caption"] = caption.strip()
        if regen is not None:
            patch["regen"] = regen
        c = self.store.edit_text(handle, md, base_sha=base_sha, meta_patch=patch)
        if c is not None:
            self._attribute_touch([c.chunk_id])
            self._sync_draft_links(c.ref_id)
        rows, cols = len(norm["rows"]), len(norm["header"])
        return Response(
            body=f"edited table {(c or chunk).dc} ({rows}×{cols}); markdown re-derived"
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
            # Flatten to one line: split() drops every whitespace run —
            # spaces, tabs, \n, \r — so a multi-line gloss stays on a single
            # outline row. No length cap: show the full gloss.
            gloss = " ".join(gloss.split())
            lines.append(f"{'  ' * c.depth}{c.dc}  [{c.chunk_kind}] {gloss}")
        lines.extend(self._work_lines(ref.id))
        return Response(body="\n".join(lines))

    def _work_lines(self, ref_id: int) -> list[str]:
        """Surface stuck / in-flight work on this draft (Fix A): the open
        todos in the draft's project subtree that are blocked by a
        failure-bubble or have a live/failed child job. Without this a
        failed enrichment job parks the parent silently and never
        registers when you look at the draft itself."""
        try:
            items = self.store.draft_attached_work(ref_id)
        except Exception:
            log.warning(
                "draft: attached-work walk failed for %s", ref_id, exc_info=True
            )
            return []
        if not items:
            return []
        out = ["", "## Work in progress"]
        for it in items:
            mark = "⚠ blocked" if it.blocked else "⚙ in flight"
            jobs = ", ".join(f"job:{jid} {st}" for jid, st in it.jobs)
            suffix = f" — {jobs}" if jobs else ""
            out.append(f"{mark}  todo:{it.todo_id}  {it.title}{suffix}")
        out.append(
            "\nNext: get(kind='todo', id=<id>) to inspect; a blocked todo "
            "carries a child-failed:<job> bubble — retry, split, or drop it "
            "(tag remove the bubble + STATUS:done) to unblock the parent."
        )
        return out

    def _render_chunk(self, addr: str) -> Response:
        # ADR 0036 relative navigation: ``dc<id>^N`` (ancestor), ``+N``/``-N``
        # (sibling step), ``-lo..hi`` (signed sibling span — the reading
        # window). Resolved against the draft tree; supersedes the legacy
        # ``-B+A`` reading-order window.
        rel = self.store.draft_relative_chunk_ids(addr)
        if rel is not None:
            if not rel:
                raise NotFound(
                    f"draft chunk {addr!r} resolves to nothing "
                    "(out of range, or no enclosing heading)"
                )
            window = [
                c
                for cid in rel
                if (c := self.store.get_draft_chunk(f"dc{cid}")) is not None
            ]
        else:
            m = _CHUNK_ADDR.match(addr)
            if m is None:
                raise BadInput(
                    f"unparseable chunk address {addr!r}",
                    next="id='dc<chunk_id>' (or dc<id>^ / +1 / -2..3 to navigate)",
                )
            # ``get_draft_chunk`` accepts ``dc<id>`` and legacy ``¶<base58>``.
            core = ("dc" + m.group("cid")) if m.group("cid") else m.group("h")
            chunk = self.store.get_draft_chunk(core)
            if chunk is None:
                raise NotFound(f"draft chunk {addr!r} not found")
            window = [chunk]
        # ``sha:`` is a short prefix of the chunk's content_sha — pass it
        # back as ``edit(base_sha=…)`` for an optimistic edit that won't
        # clobber a change that landed since this read. 12 hex chars (48
        # bits) is ample to detect a change to one chunk; the full digest
        # is needlessly long on every line. ``edit`` matches by prefix, so
        # a full 64-char sha still works.
        blocks = [
            f"{c.dc}  [{c.chunk_kind}]  sha:{content_sha(c.text)[:12]}\n{c.text}"
            for c in window
        ]
        body = "\n\n".join(blocks)
        window_text = "\n\n".join(c.text for c in window)
        body += self._dangling_finding_hint(window_text)
        body += self._dangling_chunk_hint(window_text)
        return Response(body=body)

    #: ``[finding #<slug>]`` / ``citation pending — finding #<slug>`` — the
    #: author-written placeholder form. Note this is NOT draft markup
    #: grammar (which addresses a finding as the bare ``finding:<pub_id>``
    #: mention): a ``#<slug>`` label never autolinks and never exports.
    _FINDING_MARKER = re.compile(r"finding\s+#(?P<slug>[A-Za-z][A-Za-z0-9-]+)")

    def _dangling_finding_hint(self, text: str) -> str:
        """Flag ``[finding #slug]`` markers that resolve to no finding ref
        (Fix C). The author leaves these as 'citation pending' placeholders;
        on a verbatim read they're indistinguishable from a real, linked
        citation. Resolve each marker's slug against the finding store and
        warn about the ones that don't land — so a reader can't mistake a
        placeholder for a live citation."""
        from precis.utils import mentions

        seen: list[str] = []
        dangling: list[str] = []
        for m in self._FINDING_MARKER.finditer(text):
            slug = m.group("slug")
            if slug in seen:
                continue
            seen.append(slug)
            ref = mentions.resolve_handle_ref(self.store, slug)
            if ref is None or getattr(ref, "kind", None) != "finding":
                dangling.append(slug)
        if not dangling:
            return ""
        toks = ", ".join(f"#{s}" for s in dangling)
        return (
            f"\n\n⚠ unresolved finding reference(s): {toks}. These resolve to "
            "no finding ref — they're 'citation pending' placeholders, not live "
            "citations, and won't autolink or export. For each, either create "
            "the finding (put(kind='finding', …)) and cite it by its handle "
            "(finding:<pub_id>), or remove the marker."
        )

    #: A ``[<token>]`` prose reference that *looks* like a handle attempt —
    #: a pure numeric id (the classic mistake, ``[45650]``) or a known
    #: 2-char code + digits (``[me6184]``). A non-handle ``[see note]`` is
    #: not matched, so prose stays untouched.
    _CHUNK_REF = re.compile(r"\[(?P<h>[a-z]{2}\d+|\d+)\]")

    def _dangling_chunk_hint(self, text: str) -> str:
        """Flag ``[<handle>]`` references that resolve to nothing. A handle is
        a ref to *something* (a chunk ``dc<id>``, a memory ``me<id>``, a paper
        chunk ``pc<id>``, …); an LLM that writes a numeric id (``[45650]``) or
        a typo'd handle produces a dead link. Warn here so the author fixes it
        to a handle the outline / search actually shows."""
        from precis.utils import handle_registry

        seen: list[str] = []
        dangling: list[str] = []
        for m in self._CHUNK_REF.finditer(text):
            h = m.group("h").strip()
            if h in seen:
                continue
            seen.append(h)
            # Only nag on a real handle attempt: a pure numeric, or a known
            # type-code prefix. A bare ``[ab12]`` (unknown code) is left as
            # literal prose, not flagged.
            if not h.isdigit():
                try:
                    handle_registry.kind_for_code(h[:2])
                except KeyError:
                    continue
            try:
                if self.store.resolve_handle(h) is not None:
                    continue
            except Exception:  # pragma: no cover — store hiccup, don't nag
                continue
            dangling.append(h)
        if not dangling:
            return ""
        toks = ", ".join(f"[{h}]" for h in dangling)
        return (
            f"\n\n⚠ unresolved reference(s): {toks}. A `[…]` reference must be a "
            "handle that resolves to something (a chunk `dc<id>`, a memory "
            "`me<id>`, a paper chunk `pc<id>`, …), not a numeric id — use the "
            "handle the outline / search shows, or remove the reference."
        )

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
            header = f"# TOC under {chunk.dc}: {chunk.text}"
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
                "handle": e.dc,
                "level": e.depth,
                "title": e.title,
                "gist": e.gist or (", ".join(e.keywords[:6]) if e.keywords else ""),
            }
            for e in entries
        ]
        table = toon.dump(rows, schema=["handle", "level", "title", "gist"])
        return Response(body=f"{header}\n\n{table}")
