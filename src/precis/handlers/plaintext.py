"""PlaintextHandler — read/write ``.txt`` / ``.log`` files under a root.

Sibling of :class:`precis.handlers.markdown.MarkdownHandler` with a
simpler block grammar: paragraphs only, no headings or fenced code.
Writes go to disk verbatim after a UTF-8 encode check — there is no
AST or re-parse gate beyond what markdown already does.

Same address grammar:

    notes--log          — file overview
    notes--log~SLUG     — one paragraph by slug
    notes--log~N        — one paragraph by 0-indexed pos
    notes--log/raw      — full source text
    /                   — list every known plaintext file

Same put modes (``create`` / ``append`` / ``replace`` / ``delete`` /
``edit`` / ``insert``) with identical semantics to markdown.
"""

from __future__ import annotations

import datetime
import hashlib
import logging
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from precis.dispatch import Hub, InitError
from precis.errors import BadInput, NotFound, Unsupported
from precis.protocol import Handler, KindSpec
from precis.response import Response
from precis.store import SEMANTIC_DISTANCE_FLOOR, BlockInsert, Ref
from precis.utils.edit_resolve import (
    EditOp,
    apply_edit,
    classify_diff_hunks,
    format_unified_diff,
    normalize_dry_run,
    render_dry_run_full,
    render_dry_run_header,
)
from precis.utils.md_parse import (
    file_slug_from_path,
    is_valid_file_slug,
    path_from_file_slug,
)
from precis.utils.next_block import render_next_section
from precis.utils.plaintext_parse import (
    PlaintextBlock,
    parse_plaintext,
    plaintext_extensions,
    strip_plaintext_ext,
)
from precis.utils.search_header import format_search_headline
from precis.utils.search_merge import SearchHit, block_hits_to_search_hits
from precis.utils.text import excerpt as _excerpt

log = logging.getLogger(__name__)


_SUPPORTED_VIEWS = ("raw",)
# After the seven-verb cutover, ``put`` on a file kind is creation-only.
# Region edits move to the ``edit`` verb (mode='find-replace'|'append'|
# 'insert'|'replace') and selector-deletes move to ``delete``.
_SUPPORTED_PUT_MODES = ("create",)


class PlaintextHandler(Handler):
    """Slug-addressed read/write handler for ``.txt`` / ``.log`` files."""

    spec: ClassVar[KindSpec] = KindSpec(
        kind="plaintext",
        title="Plaintext",
        description=(
            "Read and edit local plaintext files (.txt, .log) under a "
            "configured root. Lazy re-ingest on stale mtime; paragraph "
            "slugs are content-stable."
        ),
        supports_get=True,
        supports_search=True,
        supports_search_hits=True,
        supports_put=True,
        supports_edit=True,
        supports_delete=True,
        supports_tag=True,
        supports_link=True,
        is_numeric=False,
        id_required=False,
        views=_SUPPORTED_VIEWS,
        modes=_SUPPORTED_PUT_MODES,
    )

    def __init__(self, *, hub: Hub, root: Path) -> None:
        if hub.store is None:
            raise InitError("plaintext: store required")
        if not root.exists() or not root.is_dir():
            raise ValueError(
                f"plaintext root {str(root)!r} does not exist or is not a directory"
            )
        self.store = hub.store
        self.embedder = hub.embedder
        self.root = root.resolve()

    # ── get ────────────────────────────────────────────────────────

    def get(  # type: ignore[override]
        self,
        *,
        id: str | int | None = None,
        view: str | None = None,
        **_kw: Any,
    ) -> Response:
        if id is None or (isinstance(id, str) and id.startswith("/")):
            return self._render_index()

        slug, sel, path_view = _parse_pt_id(str(id))
        effective_view = path_view or view

        ref = self._ensure_ingested(slug)
        if ref is None:
            raise NotFound(
                f"plaintext file {slug!r} not found under {self.root}",
                next="get(kind='plaintext') to list every known file",
            )

        if sel is not None and effective_view is not None:
            raise BadInput(
                f"cannot combine block selector with view={effective_view!r}",
                next=f"get(kind='plaintext', id='{slug}~SLUG') or '{slug}/raw'",
            )

        if sel is not None:
            return self._render_block(ref, sel)

        if effective_view == "raw":
            return self._render_raw(ref)
        if effective_view is not None:
            raise Unsupported(
                f"unknown plaintext view {effective_view!r}",
                options=list(_SUPPORTED_VIEWS),
                next=f"get(kind='plaintext', id='{slug}/raw')",
            )

        return self._render_overview(ref)

    # ── search ─────────────────────────────────────────────────────

    def search(  # type: ignore[override]
        self,
        *,
        q: str | None = None,
        scope: str | None = None,
        top_k: int = 10,
        **_kw: Any,
    ) -> Response:
        if q is None or not q.strip():
            raise BadInput(
                "search requires q=",
                next="search(kind='plaintext', q='your query')",
            )

        scope_ref_id: int | None = None
        if scope is not None:
            scope_ref = self._ensure_ingested(scope)
            if scope_ref is None:
                raise NotFound(
                    f"plaintext file {scope!r} not found",
                    next="search(kind='plaintext', q='...') to find one",
                )
            scope_ref_id = scope_ref.id

        query_vec: list[float] | None = None
        if self.embedder is not None:
            query_vec = self.embedder.embed_one(q)

        hits = self.store.search_blocks_fused(
            q=q,
            query_vec=query_vec,
            kind="plaintext",
            scope_ref_id=scope_ref_id,
            limit=top_k,
            max_distance=SEMANTIC_DISTANCE_FLOOR,
        )
        if not hits:
            return Response(
                body=(
                    f"no plaintext blocks match {q!r}\n"
                    "next: try a broader phrase or scope='<file-slug>' "
                    "to search inside a specific file"
                )
            )

        total = self.store.count_blocks_lexical(
            q=q, kind="plaintext", scope_ref_id=scope_ref_id
        )
        lines = [
            format_search_headline(
                n_returned=len(hits),
                total=total,
                noun="block hit",
                query=q,
            )
        ]
        for block, ref, score in hits:
            slug = ref.slug or "???"
            handle = f"{slug}~{block.slug or block.pos}"
            preview = _excerpt(block.text)
            lines.append(f"\n## {handle}  (score={score:.4f})")
            lines.append(f"_{ref.title}_")
            lines.append(preview)
        return Response(body="\n".join(lines))

    # ── search_hits: structured form for cross-kind merge ──────────

    def search_hits(  # type: ignore[override]
        self,
        *,
        q: str,
        top_k: int = 10,
        **_kw: Any,
    ) -> list[SearchHit]:
        if not (q and q.strip()):
            return []
        query_vec: list[float] | None = None
        if self.embedder is not None:
            query_vec = self.embedder.embed_one(q)
        triples = self.store.search_blocks_fused(
            q=q,
            query_vec=query_vec,
            kind="plaintext",
            limit=top_k,
            max_distance=SEMANTIC_DISTANCE_FLOOR,
        )
        return block_hits_to_search_hits(triples, kind="plaintext")

    # ── put: create a new file (creation-only) ─────────────────────

    _LEGACY_PUT_MODES_TO_EDIT: ClassVar[tuple[str, ...]] = (
        "append",
        "insert",
        "replace",
        "edit",
    )

    def put(  # type: ignore[override]
        self,
        *,
        id: str | int | None = None,
        text: str | None = None,
        mode: str | None = None,
        **_kw: Any,
    ) -> Response:
        """Create a new plaintext file.

        Per the seven-verb surface (D6), ``put`` is creation-only on
        file kinds. Region edits live on the ``edit`` verb; region
        deletes live on ``delete``.
        """
        if mode in self._LEGACY_PUT_MODES_TO_EDIT:
            new_mode = "find-replace" if mode == "edit" else mode
            raise BadInput(
                f"mode={mode!r} is not accepted on put for kind='plaintext'",
                next=(
                    f"use edit(kind='plaintext', id=..., mode={new_mode!r}, ...) "
                    "for region edits"
                ),
            )
        if mode == "delete":
            raise BadInput(
                "mode='delete' is not accepted on put for kind='plaintext'",
                next="use delete(kind='plaintext', id='slug~SLUG') for region deletes",
            )
        if mode != "create":
            raise BadInput(
                f"mode= is required and must be 'create' (got {mode!r})",
                options=["create"],
                next="put(kind='plaintext', id='foo', text='...', mode='create')",
            )
        if id is None:
            raise BadInput(
                "put requires id= (the file path / slug)",
                next="put(kind='plaintext', id='foo', text='...', mode='create')",
            )
        slug, _sel, _path_view = _parse_pt_id(str(id))
        return self._put_create(slug, text)

    # ── put helpers ────────────────────────────────────────────────

    def _put_create(self, slug: str, text: str | None) -> Response:
        path = self._resolve_path(slug, must_exist=False)
        if path.exists():
            raise BadInput(
                f"file already exists: {path}",
                next=(
                    f"edit(kind='plaintext', id={slug!r}, mode='replace', "
                    "text=...) if you mean to edit"
                ),
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        body = (text or "").rstrip() + "\n"
        _atomic_write(path, body)
        ref = self._ensure_ingested(slug)
        assert ref is not None
        n = self.store.count_blocks(ref.id)
        return Response(body=f"created plaintext {slug!r} ({n} paragraph(s))")

    def _put_append(self, slug: str, text: str | None) -> Response:
        if text is None or not text.strip():
            raise BadInput(
                "append requires text=",
                next=f"put(kind='plaintext', id={slug!r}, text='...', mode='append')",
            )
        path = self._resolve_path(slug, must_exist=True)
        existing = path.read_text(encoding="utf-8")
        # Separate with a blank line so the new text reads as its own
        # paragraph on re-ingest.
        sep = "\n\n" if existing and not existing.endswith("\n\n") else ""
        if existing and not existing.endswith("\n"):
            sep = "\n\n"
        new_content = existing.rstrip() + sep + text.rstrip() + "\n"
        _atomic_write(path, new_content)
        self._ensure_ingested(slug, force=True)
        return Response(body=f"appended to plaintext {slug!r}")

    def _put_replace(
        self, slug: str, sel: _BlockSel | None, text: str | None
    ) -> Response:
        if sel is None:
            raise BadInput(
                "replace requires a block selector — id='slug~BLOCK'",
                next=(
                    f"put(kind='plaintext', id='{slug}~BLOCK', "
                    "text='...', mode='replace')"
                ),
            )
        if text is None:
            raise BadInput(
                "replace requires text=",
                next=(
                    f"put(kind='plaintext', id='{slug}~...', "
                    "text='...', mode='replace')"
                ),
            )
        path = self._resolve_path(slug, must_exist=True)
        blocks = parse_plaintext(path.read_text(encoding="utf-8"))
        target = _find_block(blocks, sel)
        if target is None:
            raise NotFound(
                f"paragraph {sel.value!r} not found in {slug!r}",
                next=f"get(kind='plaintext', id='{slug}')",
            )
        new_lines = text.rstrip("\n").split("\n")
        _replace_lines(path, target.line_start, target.line_end, new_lines)
        self._ensure_ingested(slug, force=True)
        return Response(body=f"replaced paragraph {target.slug!r} in {slug!r}")

    def _put_delete(self, slug: str, sel: _BlockSel | None) -> Response:
        if sel is None:
            raise BadInput(
                "delete requires a block selector — id='slug~BLOCK'",
                next=f"delete(kind='plaintext', id='{slug}~BLOCK')",
            )
        path = self._resolve_path(slug, must_exist=True)
        blocks = parse_plaintext(path.read_text(encoding="utf-8"))
        target = _find_block(blocks, sel)
        if target is None:
            raise NotFound(
                f"paragraph {sel.value!r} not found in {slug!r}",
                next=f"get(kind='plaintext', id='{slug}')",
            )
        _replace_lines(path, target.line_start, target.line_end, [])
        self._ensure_ingested(slug, force=True)
        return Response(body=f"deleted paragraph {target.slug!r} from {slug!r}")

    def _put_anchored(
        self,
        *,
        slug: str,
        sel: _BlockSel | None,
        op_kind: str,
        find: str | None,
        text: str | None,
        before: str,
        after: str,
        where: str | None,
        match: str,
        nth: int | None,
        dry_run: bool | str = False,
    ) -> Response:
        dry_mode = normalize_dry_run(dry_run)
        if find is None or not find:
            raise BadInput(
                f"mode={op_kind!r} requires find= (the exact text to locate)",
                next=(
                    f"put(kind='plaintext', id={slug!r}, mode={op_kind!r}, "
                    f"find='exact text', text='replacement')"
                ),
            )
        if text is None:
            raise BadInput(
                f"mode={op_kind!r} requires text= "
                "(the replacement / inserted text; '' is allowed for delete-by-edit)",
                next=(
                    "add text='...'  (use text='' on mode='edit' to delete "
                    "the matched span)"
                ),
            )
        op = EditOp(
            op="edit" if op_kind == "edit" else "insert",
            find=find,
            text=text,
            before=before,
            after=after,
            where=where,  # type: ignore[arg-type]
            match=match,  # type: ignore[arg-type]
            nth=nth,
            region_label=f"{slug}" if sel is None else f"{slug}~{sel.value}",
            base_line=1,
        )

        path = self._resolve_path(slug, must_exist=True)
        full = path.read_text(encoding="utf-8")

        if sel is None:
            result = apply_edit(full, op)
            new_full = result.new_buffer
        else:
            blocks = parse_plaintext(full)
            target = _find_block(blocks, sel)
            if target is None:
                raise NotFound(
                    f"paragraph {sel.value!r} not found in {slug!r}",
                    next=f"get(kind='plaintext', id='{slug}')",
                )
            op = EditOp(
                op=op.op,
                find=op.find,
                text=op.text,
                before=op.before,
                after=op.after,
                where=op.where,
                match=op.match,
                nth=op.nth,
                region_label=op.region_label,
                base_line=target.line_start,
            )
            result = apply_edit(target.text, op)
            new_block_lines = result.new_buffer.split("\n")
            new_full = _splice_lines(
                full, target.line_start, target.line_end, new_block_lines
            )

        if dry_mode is not None:
            return self._render_dry_run(
                slug=slug,
                sel=sel,
                pre=full,
                post=new_full,
                edited_spans=result.edited_spans,
                match_policy=op.match,
                mode=dry_mode,
            )

        # Validation gate: the post-edit buffer must round-trip through
        # UTF-8 (a Python str always does, but encoding-check the bytes
        # to catch surrogate escapes or lone surrogates that would
        # break other readers).
        try:
            new_full.encode("utf-8")
        except UnicodeEncodeError as exc:  # pragma: no cover — defensive
            raise BadInput(
                f"post-edit buffer failed UTF-8 encode: {exc}",
                next="check the replacement text for invalid Unicode",
            ) from exc

        _atomic_write(path, new_full)
        self._ensure_ingested(slug, force=True)

        spans = result.edited_spans or ()
        span_str = ", ".join(f"L{a}-{b}" if a != b else f"L{a}" for a, b in spans)
        verb = "edited" if op_kind == "edit" else "inserted"
        scope = f"{slug}~{sel.value}" if sel else slug
        summary = (
            f"{verb} {len(spans)} span{'s' if len(spans) != 1 else ''} "
            f"in {scope} ({span_str})"
        )
        return Response(body=summary)

    def _render_dry_run(
        self,
        *,
        slug: str,
        sel: _BlockSel | None,
        pre: str,
        post: str,
        edited_spans: tuple[tuple[int, int], ...],
        match_policy: str,
        mode: str,
    ) -> Response:
        region_label = f"{slug}~{sel.value}" if sel else slug
        try:
            n_blocks = len(parse_plaintext(post))
            reparse_note = f"ok (would re-ingest {n_blocks} paragraph(s))"
        except Exception as exc:  # pragma: no cover — defensive
            reparse_note = f"FAILED ({exc})"
        within, outside = classify_diff_hunks(pre, post, edited_spans)
        extras = [
            ("re-parse:", reparse_note),
            ("hunks:", f"{within} within edited spans, {outside} outside"),
        ]
        header = render_dry_run_header(
            region_label=region_label,
            edited_spans=edited_spans,
            match_policy=match_policy,
            extras=extras,
        )
        if mode == "full":
            body = render_dry_run_full(
                post,
                edited_spans=edited_spans,
                region_label=region_label,
            )
        else:
            diff = format_unified_diff(pre, post, file_label=slug).rstrip("\n")
            body = diff or "(no diff — pre and post are identical)"
        return Response(body="\n".join([*header, "", body]))

    # ── seven-verb surface ─────────────────────────────────────────

    #: Modes accepted by :meth:`edit` — region-modifying ops only.
    _EDIT_MODES: ClassVar[tuple[str, ...]] = (
        "find-replace",
        "append",
        "insert",
        "replace",
    )

    def edit(  # type: ignore[override]
        self,
        *,
        id: str | int,
        mode: str = "find-replace",
        text: str | None = None,
        find: str | None = None,
        before: str = "",
        after: str = "",
        where: str | None = None,
        match: str = "unique",
        nth: int | None = None,
        dry_run: bool | str = False,
        **_kw: Any,
    ) -> Response:
        """Region-edit an existing plaintext file.

        Same shape as :meth:`MarkdownHandler.edit`. Routes to the
        existing private helpers; ``mode='find-replace'`` is the new
        default name for the legacy ``put(mode='edit')`` path.
        """
        if mode not in self._EDIT_MODES:
            raise BadInput(
                f"unknown edit mode {mode!r}",
                options=list(self._EDIT_MODES),
                next=(
                    "edit(kind='plaintext', id='slug', mode='find-replace', "
                    "find='old', text='new')"
                ),
            )
        slug, sel, _path_view = _parse_pt_id(str(id))
        if mode == "append":
            return self._put_append(slug, text)
        if mode == "replace":
            return self._put_replace(slug, sel, text)
        op_kind = "edit" if mode == "find-replace" else mode
        return self._put_anchored(
            slug=slug,
            sel=sel,
            op_kind=op_kind,
            find=find,
            text=text,
            before=before,
            after=after,
            where=where,
            match=match,
            nth=nth,
            dry_run=dry_run,
        )

    def delete(self, *, id: str | int, **_kw: Any) -> Response:  # type: ignore[override]
        """Delete a paragraph / region from a plaintext file.

        Requires a selector in ``id`` (``slug~SLUG`` or
        ``slug~Lstart-Lend``). Whole-file delete is not exposed.
        """
        slug, sel, _path_view = _parse_pt_id(str(id))
        if sel is None:
            raise BadInput(
                f"delete on plaintext requires a block selector — id={slug!r}~SLUG",
                next=(
                    f"delete(kind='plaintext', id='{slug}~SLUG') to remove "
                    "a paragraph, or use the OS to remove the whole file"
                ),
            )
        return self._put_delete(slug, sel)

    def _resolve_pt_ref(self, id: str | int) -> tuple[str, int]:
        """Coerce an id to (slug, ref_id), ingesting the file if needed."""
        slug, sel, path_view = _parse_pt_id(str(id))
        if sel is not None or path_view is not None:
            raise BadInput(
                "plaintext tag/link ops operate at file level — drop the "
                "block selector / path view from id=",
                next=f"tag(kind='plaintext', id={slug!r}, add=[...])",
            )
        ref = self._ensure_ingested(slug)
        if ref is None:
            raise NotFound(
                f"plaintext file {slug!r} not found under {self.root}",
                next="get(kind='plaintext') to list every known file",
            )
        return slug, ref.id

    def tag(  # type: ignore[override]
        self,
        *,
        id: str | int,
        add: list[str] | None = None,
        remove: list[str] | None = None,
        **_kw: Any,
    ) -> Response:
        """Add/remove tags on a plaintext file."""
        if not add and not remove:
            raise BadInput(
                "tag(kind='plaintext', id=...) requires add= or remove=",
                next="tag(kind='plaintext', id='<slug>', add=['draft'])",
            )
        from precis.handlers._link_tag_ops import apply_tag_ops, format_link_tag_ack

        slug, ref_id = self._resolve_pt_ref(id)
        n_added, n_removed = apply_tag_ops(
            self.store, "plaintext", ref_id, tags=add, untags=remove
        )
        return Response(
            body=format_link_tag_ack(
                kind="plaintext",
                ref_label=slug,
                n_links_added=0,
                n_links_removed=0,
                n_tags_added=n_added,
                n_tags_removed=n_removed,
            )
        )

    def link(  # type: ignore[override]
        self,
        *,
        id: str | int,
        target: str | None = None,
        mode: str = "add",
        rel: str | None = None,
        **_kw: Any,
    ) -> Response:
        """Add or remove a link from a plaintext file to another ref."""
        if target is None:
            raise BadInput(
                "link(kind='plaintext', id=...) requires target=",
                next="link(kind='plaintext', id='<slug>', target='paper:slug')",
            )
        if mode not in ("add", "remove"):
            raise BadInput(
                f"link mode must be 'add' or 'remove', got {mode!r}",
                options=["add", "remove"],
            )
        from precis.handlers._link_tag_ops import apply_link_ops, format_link_tag_ack

        slug, ref_id = self._resolve_pt_ref(id)
        n_added, n_removed = apply_link_ops(
            self.store,
            ref_id,
            link=target if mode == "add" else None,
            unlink=target if mode == "remove" else None,
            rel=rel,
        )
        return Response(
            body=format_link_tag_ack(
                kind="plaintext",
                ref_label=slug,
                n_links_added=n_added,
                n_links_removed=n_removed,
                n_tags_added=0,
                n_tags_removed=0,
            )
        )

    # ── ingest pipeline ────────────────────────────────────────────

    def _ensure_ingested(self, slug: str, *, force: bool = False) -> Ref | None:
        path = self._resolve_path(slug, must_exist=False)
        ref = self.store.get_ref(kind="plaintext", id=slug)

        if not path.exists():
            if ref is not None:
                self.store.soft_delete_ref(ref.id)
            return None

        st = path.stat()
        mtime_ns = st.st_mtime_ns
        meta = (ref.meta if ref is not None else {}) or {}

        if not force and ref is not None and meta.get("mtime_ns") == mtime_ns:
            return ref

        content = path.read_text(encoding="utf-8")
        sha = hashlib.sha256(content.encode("utf-8")).hexdigest()

        if not force and ref is not None and meta.get("sha256") == sha:
            self.store.update_ref(ref.id, meta_patch={"mtime_ns": mtime_ns})
            return ref

        pt_blocks = parse_plaintext(content)
        title = _derive_title(pt_blocks, fallback=slug)
        # Preserve the original extension in meta so the handler can
        # rebuild the real file path even though both .txt and .log
        # map to the same slug shape.
        rel = str(path.relative_to(self.root))
        _base, ext = strip_plaintext_ext(rel)
        new_meta = {
            "path": rel,
            "ext": ext or ".txt",
            "mtime_ns": mtime_ns,
            "mtime_iso": datetime.datetime.fromtimestamp(
                st.st_mtime, tz=datetime.UTC
            ).isoformat(),
            "sha256": sha,
            "size": st.st_size,
        }

        embeddings = self._embed_blocks(pt_blocks)

        with self.store.tx() as conn:
            corpus_id = self.store.ensure_corpus("default")
            if ref is None:
                ref = self.store.insert_ref(
                    corpus_id=corpus_id,
                    kind="plaintext",
                    slug=slug,
                    title=title,
                    meta=new_meta,
                    conn=conn,
                )
            else:
                self.store.update_ref(ref.id, title=title, meta_patch=new_meta)

            inserts = [
                BlockInsert(
                    pos=pb.pos,
                    slug=pb.slug,
                    text=pb.text,
                    embedding=embeddings[i] if embeddings else None,
                    meta={"line_start": pb.line_start, "line_end": pb.line_end},
                )
                for i, pb in enumerate(pt_blocks)
            ]
            self.store.insert_blocks(ref.id, inserts, replace=True, conn=conn)

        return self.store.get_ref(kind="plaintext", id=slug)

    def _embed_blocks(self, blocks: list[PlaintextBlock]) -> list[list[float]] | None:
        if self.embedder is None or not blocks:
            return None
        return [self.embedder.embed_one(b.text) for b in blocks]

    # ── render helpers ─────────────────────────────────────────────

    def _render_index(self) -> Response:
        on_disk = sorted(_walk_plaintext(self.root))
        seen: dict[str, str] = {}
        for path in on_disk:
            try:
                rel = str(path.relative_to(self.root))
                base, _ext = strip_plaintext_ext(rel)
                slug = file_slug_from_path(base)
            except ValueError:
                continue
            if not is_valid_file_slug(slug):
                continue
            seen[slug] = rel

        if not seen:
            return Response(
                body=(
                    f"no plaintext files found under {self.root}\n"
                    "create one with put(kind='plaintext', id='SLUG', "
                    "text='...', mode='create')"
                )
            )

        lines = [f"# {len(seen)} plaintext file(s) under {self.root}"]
        max_w = max(len(s) for s in seen)
        for slug in sorted(seen):
            lines.append(f"  {slug:<{max_w}}  {seen[slug]}")
        body = "\n".join(lines)
        body += render_next_section(
            [
                ("get(kind='plaintext', id='<slug>')", "open a file"),
                ("get(kind='plaintext', id='<slug>/raw')", "full source"),
                (
                    "search(kind='plaintext', q='...', scope='<slug>')",
                    "search inside one file",
                ),
            ]
        )
        return Response(body=body)

    def _render_overview(self, ref: Ref) -> Response:
        meta = ref.meta or {}
        n_blocks = self.store.count_blocks(ref.id)
        rel = meta.get("path", "?")
        size = meta.get("size") or "?"
        lines = [
            f"# {ref.slug}",
            f"_{ref.title}_",
            "",
            f"path:        {rel}",
            f"paragraphs:  {n_blocks}",
            f"bytes:       {size}",
        ]
        if meta.get("mtime_iso"):
            lines.append(f"mtime:       {meta['mtime_iso']}")

        # Show the first couple of paragraph slugs so the agent has
        # something to drill into without fetching /raw.
        blocks = self.store.list_blocks_for_ref(ref.id)
        if blocks:
            lines.append("")
            lines.append("## Paragraphs (first few)")
            for b in blocks[:5]:
                preview = _excerpt(b.text, limit=60)
                lines.append(f"- ~{b.slug or b.pos}: {preview}")
            if len(blocks) > 5:
                lines.append(f"  … and {len(blocks) - 5} more (see /raw)")

        body = "\n".join(lines)
        body += render_next_section(
            [
                (f"get(kind='plaintext', id='{ref.slug}/raw')", "full source"),
                (
                    f"get(kind='plaintext', id='{ref.slug}~SLUG')",
                    "read one paragraph by slug",
                ),
                (
                    f"search(kind='plaintext', q='...', scope='{ref.slug}')",
                    "search inside this file",
                ),
            ]
        )
        return Response(body=body)

    def _render_block(self, ref: Ref, sel: _BlockSel) -> Response:
        if sel.is_pos:
            try:
                pos = int(sel.value)
            except ValueError as exc:
                raise BadInput(
                    f"unparseable pos selector: {sel.value!r}",
                    next=f"get(kind='plaintext', id='{ref.slug}~SLUG')",
                ) from exc
            block = self.store.get_block(ref.id, pos=pos)
            if block is None:
                raise NotFound(
                    f"no paragraph at ~{pos} in {ref.slug!r}",
                    next=f"get(kind='plaintext', id='{ref.slug}')",
                )
        else:
            block = self.store.get_block(ref.id, slug=sel.value)
            if block is None:
                raise NotFound(
                    f"no paragraph with slug {sel.value!r} in {ref.slug!r}",
                    next=f"get(kind='plaintext', id='{ref.slug}')",
                )
        handle = f"{ref.slug}~{block.slug or block.pos}"
        body = f"# {handle}\n{block.text}"
        body += render_next_section(
            [
                (f"get(kind='plaintext', id='{ref.slug}')", "back to overview"),
                (
                    f"edit(kind='plaintext', id='{handle}', text='...', mode='replace')",
                    "edit this paragraph",
                ),
                (
                    f"delete(kind='plaintext', id='{handle}')",
                    "delete this paragraph",
                ),
            ]
        )
        return Response(body=body)

    def _render_raw(self, ref: Ref) -> Response:
        path = self._resolve_path(ref.slug or "", must_exist=False)
        if not path.exists():
            return Response(body=f"{ref.slug}: file no longer on disk")
        return Response(body=path.read_text(encoding="utf-8"))

    # ── path resolution ────────────────────────────────────────────

    def _resolve_path(self, slug: str, *, must_exist: bool) -> Path:
        if not is_valid_file_slug(slug):
            raise BadInput(
                f"invalid plaintext slug: {slug!r}",
                next="slugs are lowercase a-z 0-9 hyphens, segments split by '--'",
            )
        # Look up the already-ingested ref to know which extension to
        # use. Fresh files default to .txt; re-reads honour the stored
        # extension so opening a .log doesn't accidentally read a .txt.
        ext = ".txt"
        existing = self.store.get_ref(kind="plaintext", id=slug)
        if existing is not None:
            ext = (existing.meta or {}).get("ext") or ".txt"
        else:
            # On first touch, probe disk for both extensions and prefer
            # whichever exists. Ties fall back to ``.txt``.
            rel_base = path_from_file_slug(slug, ext="").rstrip(".")
            for candidate_ext in plaintext_extensions():
                candidate = (self.root / (rel_base + candidate_ext)).resolve()
                try:
                    candidate.relative_to(self.root)
                except ValueError:
                    continue
                if candidate.exists():
                    ext = candidate_ext
                    break
        rel = path_from_file_slug(slug, ext=ext)
        path = (self.root / rel).resolve()
        try:
            path.relative_to(self.root)
        except ValueError as exc:
            raise BadInput(
                f"path traversal not allowed: {slug!r}",
                next="use simple slugs",
            ) from exc
        if must_exist and not path.exists():
            raise NotFound(
                f"plaintext file not found on disk: {path}",
                next=("put(kind='plaintext', id='<slug>', text='...', mode='create')"),
            )
        return path


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _BlockSel:
    value: str
    is_pos: bool


_INT_RE = re.compile(r"^\d+$")


def _parse_pt_id(raw: str) -> tuple[str, _BlockSel | None, str | None]:
    """Parse a plaintext id into ``(file_slug, block_sel, view)``.

    Accepts the same shapes as markdown:

        slug
        slug~BLOCK     — paragraph by slug
        slug~N         — paragraph by pos
        slug/raw       — view path
    """
    s = raw.strip()
    sel: _BlockSel | None = None
    view: str | None = None

    if "/" in s:
        s, _, view = s.partition("/")
        view = view.strip() or None

    if "~" in s:
        slug, _, after = s.partition("~")
        after = after.strip()
        if not after:
            raise BadInput(
                f"empty block selector in {raw!r}",
                next="slug~SLUG  or  slug~N",
            )
        is_pos = bool(_INT_RE.match(after))
        sel = _BlockSel(value=after, is_pos=is_pos)
        return slug, sel, view

    return s, sel, view


def _find_block(blocks: list[PlaintextBlock], sel: _BlockSel) -> PlaintextBlock | None:
    if sel.is_pos:
        try:
            target_pos = int(sel.value)
        except ValueError:
            return None
        for b in blocks:
            if b.pos == target_pos:
                return b
        return None
    for b in blocks:
        if b.slug == sel.value:
            return b
    return None


def _derive_title(blocks: list[PlaintextBlock], *, fallback: str) -> str:
    """Title is the first line of the first paragraph, truncated."""
    if not blocks:
        return fallback
    first_line = blocks[0].text.splitlines()[0] if blocks[0].text else ""
    first_line = first_line.strip()
    if not first_line:
        return fallback
    if len(first_line) > 80:
        return first_line[:77] + "…"
    return first_line


def _walk_plaintext(root: Path) -> list[Path]:
    """Yield every ``.txt`` / ``.log`` file under ``root``."""
    out: list[Path] = []
    exts = plaintext_extensions()
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if name.lower().endswith(exts):
                out.append(Path(dirpath) / name)
    return out


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".txt.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _splice_lines(
    raw: str, line_start: int, line_end: int, new_lines: list[str]
) -> str:
    """Replace 1-indexed inclusive ``[line_start, line_end]`` lines."""
    lines = raw.splitlines()
    lo = line_start - 1
    hi = line_end
    if new_lines:
        lines[lo:hi] = new_lines
    else:
        del lines[lo:hi]
        while (
            lo < len(lines)
            and lo > 0
            and not lines[lo].strip()
            and not lines[lo - 1].strip()
        ):
            del lines[lo]
    new_content = "\n".join(lines)
    if not new_content.endswith("\n"):
        new_content += "\n"
    return new_content


def _replace_lines(
    path: Path, line_start: int, line_end: int, new_lines: list[str]
) -> None:
    raw = path.read_text(encoding="utf-8")
    new_content = _splice_lines(raw, line_start, line_end, new_lines)
    _atomic_write(path, new_content)
