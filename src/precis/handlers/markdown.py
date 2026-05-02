"""MarkdownHandler — read/write `.md` files under a configured root.

Phase-6 first kind. The contract:

- **Address**: a ``markdown`` ref's slug encodes the file's relative
  path under the configured root. ``notes/meeting.md`` → slug
  ``notes--meeting``.
- **Blocks**: one block per logical chunk (heading line, paragraph,
  fenced-code block, table block, list block). Block slugs are
  derived from content (heading title or content hash) so they're
  stable across re-ingest.
- **Lazy re-ingest**: every ``get`` checks the source file's mtime
  against ``ref.meta.mtime``. If they differ, the file is re-read,
  re-hashed, and re-parsed; blocks are replaced atomically. This
  makes the handler always see the current version of the file
  without an explicit ingest step.
- **Put**: ``mode='append'`` adds a block at the end of the file;
  ``mode='replace'`` rewrites a single block by slug; ``mode='delete'``
  removes a block. Each call writes the file atomically and triggers
  re-ingest. ``mode='create'`` creates a new file.

Address shapes accepted by ``get`` / ``put``:

    notes--meeting           — file overview + heading TOC
    notes--meeting~SLUG      — one block by slug
    notes--meeting~N         — one block by 0-indexed pos
    notes--meeting/toc       — hierarchical table of contents
    notes--meeting/raw       — full source text
    /                        — list every known markdown file
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
from precis.handlers._paper_toc import build_toc, render_toc
from precis.protocol import Handler, KindSpec
from precis.response import Response
from precis.store import SEMANTIC_DISTANCE_FLOOR, Ref
from precis.store.types import Tag
from precis.utils.block_ingest import to_block_inserts
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
    MdBlock,
    block_meta,
    file_slug_from_path,
    is_valid_file_slug,
    parse_markdown,
    path_from_file_slug,
)
from precis.utils.next_block import render_next_section
from precis.utils.search_header import format_search_headline
from precis.utils.search_merge import SearchHit, block_hits_to_search_hits
from precis.utils.text import excerpt as _excerpt

log = logging.getLogger(__name__)


_SUPPORTED_VIEWS = ("toc", "raw")
# After the seven-verb cutover, ``put`` on a file kind is creation-only.
# Region edits move to the ``edit`` verb (mode='find-replace'|'append'|
# 'insert'|'replace') and selector-deletes move to ``delete``. The
# ``create`` mode survives on put as the canonical verb-to-create-a-
# new-file shape.
_SUPPORTED_PUT_MODES = ("create",)


# ---------------------------------------------------------------------------
# Public spec
# ---------------------------------------------------------------------------


class MarkdownHandler(Handler):
    """Slug-addressed read/write handler for ``.md`` files."""

    spec: ClassVar[KindSpec] = KindSpec(
        kind="markdown",
        title="Markdown",
        description=(
            "Read and edit local markdown files under a configured root. "
            "Lazy re-ingest on stale mtime; block slugs are content-stable."
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
            raise InitError("markdown: store required")
        if not root.exists() or not root.is_dir():
            raise ValueError(
                f"markdown root {str(root)!r} does not exist or is not a directory"
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

        slug, sel, path_view = _parse_md_id(str(id))
        effective_view = path_view or view

        ref = self._ensure_ingested(slug)
        if ref is None:
            raise NotFound(
                f"markdown file {slug!r} not found under PRECIS_ROOT",
                next="get(kind='markdown') to list every known file",
            )

        if sel is not None and effective_view is not None:
            raise BadInput(
                f"cannot combine block selector with view={effective_view!r}",
                next=f"get(kind='markdown', id='{slug}~SLUG') or '{slug}/toc'",
            )

        if sel is not None:
            return self._render_block(ref, sel)

        if effective_view == "toc":
            return self._render_toc(ref)
        if effective_view == "raw":
            return self._render_raw(ref)
        if effective_view is not None:
            raise Unsupported(
                f"unknown markdown view {effective_view!r}",
                options=list(_SUPPORTED_VIEWS),
                next=f"get(kind='markdown', id='{slug}/toc')",
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
                next="search(kind='markdown', q='your query')",
            )

        scope_ref_id: int | None = None
        if scope is not None:
            scope_ref = self._ensure_ingested(scope)
            if scope_ref is None:
                raise NotFound(
                    f"markdown file {scope!r} not found",
                    next="search(kind='markdown', q='...') to find one",
                )
            scope_ref_id = scope_ref.id

        query_vec: list[float] | None = None
        if self.embedder is not None:
            query_vec = self.embedder.embed_one(q)

        hits = self.store.search_blocks_fused(
            q=q,
            query_vec=query_vec,
            kind="markdown",
            scope_ref_id=scope_ref_id,
            limit=top_k,
            max_distance=SEMANTIC_DISTANCE_FLOOR,
        )
        if not hits:
            # Canonical Next: block — c5 unified-trailer patch.
            body = f"no markdown blocks match {q!r}"
            body += render_next_section(
                [
                    (
                        f"search(kind='markdown', q={q!r}, top_k=50)",
                        "widen the lexical net",
                    ),
                    (
                        f"search(kind='markdown', q={q!r}, scope='<file-slug>')",
                        "search inside a specific note",
                    ),
                ]
            )
            return Response(body=body)

        # Total-hits header — see precis.utils.search_header for
        # the wording rationale. Lexical-only count: fused search
        # ranks lexical matches by RRF, so the lexical universe is
        # the meaningful "K".
        total = self.store.count_blocks_lexical(
            q=q, kind="markdown", scope_ref_id=scope_ref_id
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
        """Block-level fused search returned as ``SearchHit``s.

        Same engine as :meth:`search` but skips path-scoped lookups
        — cross-kind merge has no per-file scope.
        """
        if not (q and q.strip()):
            return []
        query_vec: list[float] | None = None
        if self.embedder is not None:
            query_vec = self.embedder.embed_one(q)
        triples = self.store.search_blocks_fused(
            q=q,
            query_vec=query_vec,
            kind="markdown",
            limit=top_k,
            max_distance=SEMANTIC_DISTANCE_FLOOR,
        )
        return block_hits_to_search_hits(triples, kind="markdown")

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
        """Create a new markdown file.

        Per the seven-verb surface (D6), ``put`` is creation-only on
        file kinds. Region edits (append / insert / replace / find-
        replace) live on the ``edit`` verb; region deletes live on
        ``delete``.
        """
        if mode in self._LEGACY_PUT_MODES_TO_EDIT:
            new_mode = "find-replace" if mode == "edit" else mode
            raise BadInput(
                f"mode={mode!r} is not accepted on put for kind='markdown'",
                next=(
                    f"use edit(kind='markdown', id=..., mode={new_mode!r}, ...) "
                    "for region edits"
                ),
            )
        if mode == "delete":
            raise BadInput(
                "mode='delete' is not accepted on put for kind='markdown'",
                next="use delete(kind='markdown', id='slug~BLOCK') for region deletes",
            )
        if mode != "create":
            raise BadInput(
                f"mode= is required and must be 'create' (got {mode!r})",
                options=["create"],
                next="put(kind='markdown', id='foo', text='# Title\\n', mode='create')",
            )
        if id is None:
            raise BadInput(
                "put requires id= (the file path / slug)",
                next="put(kind='markdown', id='foo', text='# Title\\n', mode='create')",
            )
        slug, _sel, _path_view = _parse_md_id(str(id))
        return self._put_create(slug, text)

    # ── put helpers ────────────────────────────────────────────────

    def _put_create(self, slug: str, text: str | None) -> Response:
        path = self._resolve_path(slug, must_exist=False)
        if path.exists():
            raise BadInput(
                f"file already exists: {slug!r}",
                next=f"edit(kind='markdown', id={slug!r}, mode='replace', text=...) if you mean to edit",
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        body = (text or "").rstrip() + "\n"
        _atomic_write(path, body)
        ref = self._ensure_ingested(slug)
        assert ref is not None
        return Response(
            body=f"created markdown {slug!r} ({self.store.count_blocks(ref.id)} blocks)"
        )

    def _put_append(self, slug: str, text: str | None) -> Response:
        if text is None or not text.strip():
            raise BadInput(
                "append requires text=",
                next=f"put(kind='markdown', id={slug!r}, text='...', mode='append')",
            )
        path = self._resolve_path(slug, must_exist=True)
        existing = path.read_text(encoding="utf-8")
        # Always separate the new block with a blank line.
        sep = "\n\n" if existing and not existing.endswith("\n\n") else ""
        if existing and not existing.endswith("\n"):
            sep = "\n\n"
        new_content = existing.rstrip() + sep + text.rstrip() + "\n"
        _atomic_write(path, new_content)
        ref = self._ensure_ingested(slug, force=True)
        assert ref is not None
        return Response(body=f"appended to markdown {slug!r}")

    def _put_replace(
        self, slug: str, sel: _BlockSel | None, text: str | None
    ) -> Response:
        if sel is None:
            raise BadInput(
                "replace requires a block selector — id='slug~BLOCK'",
                next=f"put(kind='markdown', id='{slug}~BLOCK', text='...', mode='replace')",
            )
        if text is None:
            raise BadInput(
                "replace requires text=",
                next=f"put(kind='markdown', id='{slug}~...', text='...', mode='replace')",
            )
        path = self._resolve_path(slug, must_exist=True)
        blocks = parse_markdown(path.read_text(encoding="utf-8"))
        target = _find_block(blocks, sel)
        if target is None:
            raise NotFound(
                f"block {sel.value!r} not found in {slug!r}",
                next=f"get(kind='markdown', id='{slug}/toc')",
            )
        new_lines = text.rstrip("\n").split("\n")
        _replace_lines(path, target.line_start, target.line_end, new_lines)
        self._ensure_ingested(slug, force=True)
        return Response(body=f"replaced block {target.slug!r} in {slug!r}")

    def _put_delete(self, slug: str, sel: _BlockSel | None) -> Response:
        if sel is None:
            raise BadInput(
                "delete requires a block selector — id='slug~BLOCK'",
                next=f"delete(kind='markdown', id='{slug}~BLOCK')",
            )
        path = self._resolve_path(slug, must_exist=True)
        blocks = parse_markdown(path.read_text(encoding="utf-8"))
        target = _find_block(blocks, sel)
        if target is None:
            raise NotFound(
                f"block {sel.value!r} not found in {slug!r}",
                next=f"get(kind='markdown', id='{slug}/toc')",
            )
        _replace_lines(path, target.line_start, target.line_end, [])
        self._ensure_ingested(slug, force=True)
        return Response(body=f"deleted block {target.slug!r} from {slug!r}")

    def _put_anchored(
        self,
        *,
        slug: str,
        sel: _BlockSel | None,
        op_kind: str,  # "edit" or "insert"
        find: str | None,
        text: str | None,
        before: str,
        after: str,
        where: str | None,
        match: str,
        nth: int | None,
        dry_run: bool | str = False,
    ) -> Response:
        """Anchored find/replace (mode='edit') or insert (mode='insert').

        Region is the whole file when no selector is given, or the
        addressed block's text when ``id='slug~BLOCK'`` is supplied.
        Content selects via :func:`apply_edit`; the result is spliced
        back into the file and the file is re-ingested.

        ``dry_run`` short-circuits the disk write and re-ingest; the
        agent gets the proposed diff (or post-edit region) plus
        validation results. Disk is untouched.

        Errors come from ``apply_edit`` (not-found, ambiguous, no-op)
        and from selector resolution (block missing). All errors are
        ``BadInput`` / ``NotFound`` with actionable ``next=`` hints.
        """
        dry_mode = normalize_dry_run(dry_run)
        if find is None or not find:
            raise BadInput(
                f"mode={op_kind!r} requires find= (the exact text to locate)",
                next=(
                    f"put(kind='markdown', id={slug!r}, mode={op_kind!r}, "
                    f"find='exact text', text='replacement')"
                ),
            )
        if text is None:
            raise BadInput(
                f"mode={op_kind!r} requires text= (the replacement / inserted text; '' is allowed for delete-by-edit)",
                next="add text='...'  (use text='' on mode='edit' to delete the matched span)",
            )
        # Build the EditOp early so its validators throw with sharp
        # error messages (e.g. unknown match policy, missing where=).
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
            base_line=1,  # patched below if sel is given
        )

        path = self._resolve_path(slug, must_exist=True)
        full = path.read_text(encoding="utf-8")

        if sel is None:
            # Whole-file edit. apply_edit operates on the full buffer.
            result = apply_edit(full, op)
            new_full = result.new_buffer
        else:
            # Block-scoped edit. Resolve the block, run apply_edit
            # against its text only, splice back into the full file.
            blocks = parse_markdown(full)
            target = _find_block(blocks, sel)
            if target is None:
                raise NotFound(
                    f"block {sel.value!r} not found in {slug!r}",
                    next=f"get(kind='markdown', id='{slug}/toc')",
                )
            # Re-build EditOp with the absolute base_line so error
            # messages cite file lines, not block-relative lines.
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
            # ``MdBlock.text`` is the lines joined with '\n' (no
            # trailing newline). The pure splice handles the file's
            # trailing-newline preservation.
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

        # Real write — atomic, then re-ingest.
        _atomic_write(path, new_full)
        self._ensure_ingested(slug, force=True)

        # Build a short summary line per edited span.
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
        mode: str,  # 'diff' or 'full'
    ) -> Response:
        """Render a dry-run response: header + (diff | full).

        ``mode='diff'`` emits a unified diff against the full file;
        ``mode='full'`` emits the post-edit lines around each span
        with leading-line markers (``> `` for edited, blank for
        context). The header lines are identical in both cases.
        """
        region_label = f"{slug}~{sel.value}" if sel else slug
        # Markdown re-parses on every ingest — cheap to confirm here.
        try:
            n_blocks = len(parse_markdown(post))
            reparse_note = f"ok (would re-ingest {n_blocks} block(s))"
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
    #: ``create`` lives on ``put`` (it makes a new file, not a region
    #: edit). ``delete`` lives on the dedicated ``delete`` verb.
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
        """Region-edit an existing markdown file.

        Routes to the same private helpers the legacy ``put(mode=...)``
        path used. ``mode='find-replace'`` is the new default
        (formerly ``put(mode='edit')``); the other modes keep their
        names.
        """
        if mode not in self._EDIT_MODES:
            raise BadInput(
                f"unknown edit mode {mode!r}",
                options=list(self._EDIT_MODES),
                next=(
                    "edit(kind='markdown', id='slug', mode='find-replace', "
                    "find='old', text='new')"
                ),
            )
        slug, sel, _path_view = _parse_md_id(str(id))
        if mode == "append":
            return self._put_append(slug, text)
        if mode == "replace":
            return self._put_replace(slug, sel, text)
        # find-replace and insert both go through the anchored helper.
        # The helper used "edit" as the legacy mode name for find-
        # replace; map it back here so the inner code stays unchanged.
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
        """Delete a block / region from a markdown file.

        Requires a selector in ``id`` (``slug~BLOCK`` or
        ``slug~Lstart-Lend``). Whole-file delete is not exposed —
        use the OS or ``edit(mode='replace', text='')`` if you need
        to clear a file.
        """
        slug, sel, _path_view = _parse_md_id(str(id))
        if sel is None:
            raise BadInput(
                f"delete on markdown requires a block selector — id={slug!r}~BLOCK",
                next=(
                    f"delete(kind='markdown', id='{slug}~BLOCK') to remove a "
                    "block, or use the OS to remove the whole file"
                ),
            )
        return self._put_delete(slug, sel)

    def _resolve_md_ref(self, id: str | int) -> tuple[str, int]:
        """Coerce an id to (slug, ref_id), ingesting the file if needed.

        Tag/link ops are ref-level, so a chunk selector or path view in
        ``id=`` is rejected.
        """
        slug, sel, path_view = _parse_md_id(str(id))
        if sel is not None or path_view is not None:
            raise BadInput(
                "markdown tag/link ops operate at file level — drop the "
                "block selector / path view from id=",
                next=f"tag(kind='markdown', id={slug!r}, add=[...])",
            )
        ref = self._ensure_ingested(slug)
        if ref is None:
            raise NotFound(
                f"markdown file {slug!r} not found under PRECIS_ROOT",
                next="get(kind='markdown') to list every known file",
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
        """Add/remove tags on a markdown file."""
        if not add and not remove:
            raise BadInput(
                "tag(kind='markdown', id=...) requires add= or remove=",
                next="tag(kind='markdown', id='<slug>', add=['draft'])",
            )
        from precis.handlers._link_tag_ops import apply_tag_ops, format_link_tag_ack

        slug, ref_id = self._resolve_md_ref(id)
        n_added, n_removed = apply_tag_ops(
            self.store, "markdown", ref_id, tags=add, untags=remove
        )
        return Response(
            body=format_link_tag_ack(
                kind="markdown",
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
        """Add or remove a link from a markdown file to another ref."""
        if target is None:
            raise BadInput(
                "link(kind='markdown', id=...) requires target=",
                next="link(kind='markdown', id='<slug>', target='paper:slug')",
            )
        if mode not in ("add", "remove"):
            raise BadInput(
                f"link mode must be 'add' or 'remove', got {mode!r}",
                options=["add", "remove"],
            )
        from precis.handlers._link_tag_ops import apply_link_ops, format_link_tag_ack

        slug, ref_id = self._resolve_md_ref(id)
        n_added, n_removed = apply_link_ops(
            self.store,
            ref_id,
            link=target if mode == "add" else None,
            unlink=target if mode == "remove" else None,
            rel=rel,
        )
        return Response(
            body=format_link_tag_ack(
                kind="markdown",
                ref_label=slug,
                n_links_added=n_added,
                n_links_removed=n_removed,
                n_tags_added=0,
                n_tags_removed=0,
            )
        )

    # ── ingest pipeline ────────────────────────────────────────────

    # Flag tag auto-applied to every ingested ref so the LLM can scope
    # ``search(tags=['workspace'])`` to its ``PRECIS_ROOT``. See
    # :meth:`PlaintextHandler._apply_workspace_tag` for the contract
    # (idempotent INSERT, ``set_by='system'``).
    _WORKSPACE_FLAG: ClassVar[str] = "workspace"

    def _apply_workspace_tag(self, ref: Ref) -> None:
        self.store.add_tag(
            ref.id,
            Tag.flag(self._WORKSPACE_FLAG),
            set_by="system",
        )

    def _ensure_ingested(self, slug: str, *, force: bool = False) -> Ref | None:
        """Materialize the file at ``slug`` into the store if needed.

        Returns the up-to-date ref, or None if the file doesn't exist
        on disk (and isn't already in the store).
        """
        path = self._resolve_path(slug, must_exist=False)
        ref = self.store.get_ref(kind="markdown", id=slug)

        if not path.exists():
            if ref is not None:
                # File deleted on disk → soft-delete the ref so listings
                # don't surface ghost entries.
                self.store.soft_delete_ref(ref.id)
            return None

        # Cheap freshness check: compare mtime fingerprint.
        st = path.stat()
        mtime_ns = st.st_mtime_ns
        meta = (ref.meta if ref is not None else {}) or {}

        if not force and ref is not None and meta.get("mtime_ns") == mtime_ns:
            self._apply_workspace_tag(ref)
            return ref

        # Slow path — re-read and re-hash.
        content = path.read_text(encoding="utf-8")
        sha = hashlib.sha256(content.encode("utf-8")).hexdigest()

        if not force and ref is not None and meta.get("sha256") == sha:
            # Same content, just touched. Bump mtime in meta and bail.
            self.store.update_ref(ref.id, meta_patch={"mtime_ns": mtime_ns})
            self._apply_workspace_tag(ref)
            return ref

        # Re-parse and replace blocks.
        md_blocks = parse_markdown(content)
        title = _derive_title(md_blocks, fallback=slug)
        new_meta = {
            "path": str(path.relative_to(self.root)),
            "mtime_ns": mtime_ns,
            "mtime_iso": datetime.datetime.fromtimestamp(
                st.st_mtime, tz=datetime.UTC
            ).isoformat(),
            "sha256": sha,
            "size": st.st_size,
        }

        inserts = to_block_inserts(
            md_blocks, embedder=self.embedder, meta_for=block_meta
        )

        with self.store.tx() as conn:
            corpus_id = self.store.ensure_corpus("default")
            if ref is None:
                ref = self.store.insert_ref(
                    corpus_id=corpus_id,
                    kind="markdown",
                    slug=slug,
                    title=title,
                    meta=new_meta,
                    conn=conn,
                )
            else:
                self.store.update_ref(ref.id, title=title, meta_patch=new_meta)

            self.store.insert_blocks(ref.id, inserts, replace=True, conn=conn)

        # Re-fetch to pick up the patched meta + new title.
        refreshed = self.store.get_ref(kind="markdown", id=slug)
        if refreshed is not None:
            self._apply_workspace_tag(refreshed)
        return refreshed

    # ── render helpers ─────────────────────────────────────────────

    def _render_index(self) -> Response:
        # Discover files on disk (canonical), but also surface refs that
        # exist in the store.
        on_disk = sorted(_walk_md(self.root))
        seen: dict[str, str] = {}
        for path in on_disk:
            try:
                rel = str(path.relative_to(self.root))
                slug = file_slug_from_path(rel)
            except ValueError:
                continue
            if not is_valid_file_slug(slug):
                continue
            seen[slug] = rel

        if not seen:
            return Response(
                body=(
                    "no markdown files found under PRECIS_ROOT\n"
                    "create one with put(kind='markdown', id='SLUG', text='# Title\\n...', mode='create')"
                )
            )

        lines = [f"# {len(seen)} markdown file(s) under PRECIS_ROOT"]
        max_w = max(len(s) for s in seen)
        for slug in sorted(seen):
            lines.append(f"  {slug:<{max_w}}  {seen[slug]}")
        body = "\n".join(lines)
        body += render_next_section(
            [
                ("get(kind='markdown', id='<slug>')", "open a file"),
                ("get(kind='markdown', id='<slug>/toc')", "table of contents"),
                (
                    "search(kind='markdown', q='...', scope='<slug>')",
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
            f"path:    {rel}",
            f"blocks:  {n_blocks}",
            f"bytes:   {size}",
        ]
        if meta.get("mtime_iso"):
            lines.append(f"mtime:   {meta['mtime_iso']}")

        # Inline a short heading TOC if there are headings.
        blocks = self.store.list_blocks_for_ref(ref.id)
        toc = build_toc(blocks)
        # Flatten the section tree (H1 + nested H2s) and filter
        # implicit untitled sections — those are noise in the inline
        # preview.
        flat: list = []
        for s in toc:
            if s.title:
                flat.append(s)
            for child in s.children:
                if child.title:
                    flat.append(child)
        if flat:
            lines.append("")
            lines.append("## Headings")
            for entry in flat[:10]:
                indent = "  " * max(entry.level - 1, 0)
                lines.append(f"{indent}- ~{entry.start} {entry.title}")
            if len(flat) > 10:
                lines.append(f"  … and {len(flat) - 10} more (see /toc)")

        body = "\n".join(lines)
        body += render_next_section(
            [
                (f"get(kind='markdown', id='{ref.slug}/toc')", "full TOC"),
                (f"get(kind='markdown', id='{ref.slug}/raw')", "full source"),
                (
                    f"get(kind='markdown', id='{ref.slug}~SLUG')",
                    "read one block by slug",
                ),
                (
                    f"search(kind='markdown', q='...', scope='{ref.slug}')",
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
                    next=f"get(kind='markdown', id='{ref.slug}~SLUG')",
                ) from exc
            block = self.store.get_block(ref.id, pos=pos)
            if block is None:
                raise NotFound(
                    f"no block at ~{pos} in {ref.slug!r}",
                    next=f"get(kind='markdown', id='{ref.slug}/toc')",
                )
        else:
            block = self.store.get_block(ref.id, slug=sel.value)
            if block is None:
                raise NotFound(
                    f"no block with slug {sel.value!r} in {ref.slug!r}",
                    next=f"get(kind='markdown', id='{ref.slug}/toc')",
                )
        handle = f"{ref.slug}~{block.slug or block.pos}"
        body = f"# {handle}\n{block.text}"
        body += render_next_section(
            [
                (f"get(kind='markdown', id='{ref.slug}')", "back to overview"),
                (
                    f"edit(kind='markdown', id='{handle}', text='...', mode='replace')",
                    "edit this block",
                ),
                (
                    f"delete(kind='markdown', id='{handle}')",
                    "delete this block",
                ),
            ]
        )
        return Response(body=body)

    def _render_toc(self, ref: Ref) -> Response:
        blocks = self.store.list_blocks_for_ref(ref.id)
        if not blocks:
            return Response(body=f"{ref.slug}: no blocks indexed")
        toc = build_toc(blocks)
        if not toc or not any(s.title for s in toc):
            return Response(body=f"# {ref.slug}\n_{ref.title}_\n\nno headings")
        blocks_by_pos = {b.pos: b for b in blocks}
        body = render_toc(
            slug=ref.slug or "?",
            toc=toc,
            total_blocks=len(blocks),
            blocks_by_pos=blocks_by_pos,
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
                f"invalid markdown slug: {slug!r}",
                next="slugs are lowercase a-z 0-9 hyphens, segments split by '--'",
            )
        rel = path_from_file_slug(slug, ext=".md")
        path = (self.root / rel).resolve()
        # Defence-in-depth: ensure the resolved path is under the root.
        try:
            path.relative_to(self.root)
        except ValueError as exc:
            raise BadInput(
                f"path traversal not allowed: {slug!r}",
                next="use simple slugs",
            ) from exc
        if must_exist and not path.exists():
            raise NotFound(
                f"markdown file {slug!r} not found on disk",
                next="put(kind='markdown', id='<slug>', text='...', mode='create')",
            )
        return path


# ---------------------------------------------------------------------------
# Module-level helpers (parsing, file I/O)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _BlockSel:
    value: str
    is_pos: bool


_INT_RE = re.compile(r"^\d+$")


def _parse_md_id(raw: str) -> tuple[str, _BlockSel | None, str | None]:
    """Parse a markdown id into ``(file_slug, block_sel, view)``.

    Accepts:
        slug
        slug~BLOCK     — block by slug
        slug~N         — block by pos (digits-only)
        slug/toc       — view path
        slug/raw
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


def _find_block(blocks: list[MdBlock], sel: _BlockSel) -> MdBlock | None:
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


def _derive_title(blocks: list[MdBlock], *, fallback: str) -> str:
    """Title = first H1, else first heading, else the file slug."""
    for b in blocks:
        if b.kind == "heading" and b.heading_level == 1:
            return b.text.lstrip("#").strip()
    for b in blocks:
        if b.kind == "heading":
            return b.text.lstrip("#").strip()
    return fallback


def _walk_md(root: Path) -> list[Path]:
    """Yield every ``.md`` file under ``root`` (sorted, deterministic)."""
    out: list[Path] = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if name.endswith((".md", ".markdown")):
                out.append(Path(dirpath) / name)
    return out


def _atomic_write(path: Path, content: str) -> None:
    """Write `content` to `path` atomically (tmpfile + rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".md.tmp")
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
    """Pure splice — return ``raw`` with ``[line_start, line_end]``
    replaced by ``new_lines`` (1-indexed inclusive).

    If ``new_lines`` is empty, the lines are deleted (and any trailing
    blank line is collapsed so we don't grow a stack of empty blanks).
    Used by both the live write path and ``dry_run`` to materialise
    the post-edit buffer without touching disk.
    """
    lines = raw.splitlines()
    # 1-indexed inclusive → slice indices.
    lo = line_start - 1
    hi = line_end
    if new_lines:
        lines[lo:hi] = new_lines
    else:
        del lines[lo:hi]
        # Collapse the now-merged blank gap (one blank is enough).
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
    """Replace 1-indexed inclusive ``[line_start, line_end]`` with new content.

    If ``new_lines`` is empty, the lines are deleted (and any trailing
    blank line is collapsed so we don't grow a stack of empty blanks).
    """
    raw = path.read_text(encoding="utf-8")
    new_content = _splice_lines(raw, line_start, line_end, new_lines)
    _atomic_write(path, new_content)
