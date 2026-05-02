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
from precis.store import SEMANTIC_DISTANCE_FLOOR, Ref
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
    file_slug_from_path,
    is_valid_file_slug,
    path_from_file_slug,
)
from precis.utils.next_block import render_next_section
from precis.utils.plaintext_parse import (
    PlaintextBlock,
    parse_plaintext,
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

    # ── subclass-extensible knobs ───────────────────────────────────
    # Sibling kinds (e.g. ``tex``) reuse the entire paragraph-block
    # pipeline by overriding these ClassVars + the ``spec`` above.
    # No instance methods need re-implementing.
    _KIND: ClassVar[str] = "plaintext"
    _EXTENSIONS: ClassVar[tuple[str, ...]] = (".txt", ".log")
    _DEFAULT_EXT: ClassVar[str] = ".txt"

    @classmethod
    def _strip_ext(cls, rel_path: str) -> tuple[str, str]:
        """Return ``(base_without_extension, extension)`` for one of
        :attr:`_EXTENSIONS`. Unrecognised extensions yield an empty
        ``extension`` so the caller can reject them."""
        for candidate in cls._EXTENSIONS:
            if rel_path.lower().endswith(candidate):
                return rel_path[: -len(candidate)], rel_path[-len(candidate) :].lower()
        return rel_path, ""

    def _walk_files(self) -> list[Path]:
        """Yield every file under ``self.root`` whose extension is in
        :attr:`_EXTENSIONS` (case-insensitive)."""
        out: list[Path] = []
        exts = tuple(e.lower() for e in self._EXTENSIONS)
        for dirpath, _dirnames, filenames in os.walk(self.root):
            for name in filenames:
                if name.lower().endswith(exts):
                    out.append(Path(dirpath) / name)
        return out

    def __init__(self, *, hub: Hub, root: Path) -> None:
        if hub.store is None:
            raise InitError(f"{self._KIND}: store required")
        if not root.exists() or not root.is_dir():
            raise ValueError(
                f"{self._KIND} root {str(root)!r} does not exist or is not a directory"
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
                f"{self._KIND} file {slug!r} not found under {self.root}",
                next=f"get(kind='{self._KIND}') to list every known file",
            )

        if sel is not None and effective_view is not None:
            raise BadInput(
                f"cannot combine block selector with view={effective_view!r}",
                next=f"get(kind='{self._KIND}', id='{slug}~SLUG') or '{slug}/raw'",
            )

        if sel is not None:
            return self._render_block(ref, sel)

        if effective_view == "raw":
            return self._render_raw(ref)
        if effective_view is not None:
            raise Unsupported(
                f"unknown {self._KIND} view {effective_view!r}",
                options=list(_SUPPORTED_VIEWS),
                next=f"get(kind='{self._KIND}', id='{slug}/raw')",
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
                next=f"search(kind='{self._KIND}', q='your query')",
            )

        scope_ref_id: int | None = None
        if scope is not None:
            scope_ref = self._ensure_ingested(scope)
            if scope_ref is None:
                raise NotFound(
                    f"{self._KIND} file {scope!r} not found",
                    next=f"search(kind='{self._KIND}', q='...') to find one",
                )
            scope_ref_id = scope_ref.id

        query_vec: list[float] | None = None
        if self.embedder is not None:
            query_vec = self.embedder.embed_one(q)

        hits = self.store.search_blocks_fused(
            q=q,
            query_vec=query_vec,
            kind=self._KIND,
            scope_ref_id=scope_ref_id,
            limit=top_k,
            max_distance=SEMANTIC_DISTANCE_FLOOR,
        )
        if not hits:
            # Canonical Next: block — c5 unified-trailer patch.
            body = f"no {self._KIND} blocks match {q!r}"
            body += render_next_section(
                [
                    (
                        f"search(kind='{self._KIND}', q={q!r}, top_k=50)",
                        "widen the lexical net",
                    ),
                    (
                        f"search(kind='{self._KIND}', q={q!r}, scope='<file-slug>')",
                        "search inside a specific file",
                    ),
                ]
            )
            return Response(body=body)

        total = self.store.count_blocks_lexical(
            q=q, kind=self._KIND, scope_ref_id=scope_ref_id
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
            kind=self._KIND,
            limit=top_k,
            max_distance=SEMANTIC_DISTANCE_FLOOR,
        )
        return block_hits_to_search_hits(triples, kind=self._KIND)

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
        """Create a new paragraph-block file.

        Per the seven-verb surface (D6), ``put`` is creation-only on
        file kinds. Region edits live on the ``edit`` verb; region
        deletes live on ``delete``.
        """
        if mode in self._LEGACY_PUT_MODES_TO_EDIT:
            new_mode = "find-replace" if mode == "edit" else mode
            raise BadInput(
                f"mode={mode!r} is not accepted on put for kind={self._KIND!r}",
                next=(
                    f"use edit(kind='{self._KIND}', id=..., mode={new_mode!r}, ...) "
                    "for region edits"
                ),
            )
        if mode == "delete":
            raise BadInput(
                f"mode='delete' is not accepted on put for kind={self._KIND!r}",
                next=(
                    f"use delete(kind='{self._KIND}', id='slug~SLUG') for region "
                    "deletes"
                ),
            )
        if mode != "create":
            raise BadInput(
                f"mode= is required and must be 'create' (got {mode!r})",
                options=["create"],
                next=f"put(kind='{self._KIND}', id='foo', text='...', mode='create')",
            )
        if id is None:
            raise BadInput(
                "put requires id= (the file path / slug)",
                next=f"put(kind='{self._KIND}', id='foo', text='...', mode='create')",
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
                    f"edit(kind='{self._KIND}', id={slug!r}, mode='replace', "
                    "text=...) if you mean to edit"
                ),
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        body = (text or "").rstrip() + "\n"
        _atomic_write(path, body)
        ref = self._ensure_ingested(slug)
        assert ref is not None
        n = self.store.count_blocks(ref.id)
        return Response(body=f"created {self._KIND} {slug!r} ({n} paragraph(s))")

    def _put_append(self, slug: str, text: str | None) -> Response:
        if text is None or not text.strip():
            raise BadInput(
                "append requires text=",
                next=(
                    f"put(kind='{self._KIND}', id={slug!r}, text='...', mode='append')"
                ),
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
        return Response(body=f"appended to {self._KIND} {slug!r}")

    def _put_replace(
        self, slug: str, sel: _BlockSel | None, text: str | None
    ) -> Response:
        if sel is None:
            raise BadInput(
                "replace requires a block selector — id='slug~BLOCK'",
                next=(
                    f"put(kind='{self._KIND}', id='{slug}~BLOCK', "
                    "text='...', mode='replace')"
                ),
            )
        if text is None:
            raise BadInput(
                "replace requires text=",
                next=(
                    f"put(kind='{self._KIND}', id='{slug}~...', "
                    "text='...', mode='replace')"
                ),
            )
        path = self._resolve_path(slug, must_exist=True)
        blocks = parse_plaintext(path.read_text(encoding="utf-8"))
        target = _find_block(blocks, sel)
        if target is None:
            raise NotFound(
                f"paragraph {sel.value!r} not found in {slug!r}",
                next=f"get(kind='{self._KIND}', id='{slug}')",
            )
        new_lines = text.rstrip("\n").split("\n")
        _replace_lines(path, target.line_start, target.line_end, new_lines)
        self._ensure_ingested(slug, force=True)
        return Response(body=f"replaced paragraph {target.slug!r} in {slug!r}")

    def _put_delete(self, slug: str, sel: _BlockSel | None) -> Response:
        if sel is None:
            raise BadInput(
                "delete requires a block selector — id='slug~BLOCK'",
                next=f"delete(kind='{self._KIND}', id='{slug}~BLOCK')",
            )
        path = self._resolve_path(slug, must_exist=True)
        blocks = parse_plaintext(path.read_text(encoding="utf-8"))
        target = _find_block(blocks, sel)
        if target is None:
            raise NotFound(
                f"paragraph {sel.value!r} not found in {slug!r}",
                next=f"get(kind='{self._KIND}', id='{slug}')",
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
                    f"put(kind='{self._KIND}', id={slug!r}, mode={op_kind!r}, "
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
                    next=f"get(kind='{self._KIND}', id='{slug}')",
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
        """Region-edit an existing paragraph-block file.

        Same shape as :meth:`MarkdownHandler.edit`. Routes to the
        existing private helpers; ``mode='find-replace'`` is the new
        default name for the legacy ``put(mode='edit')`` path.
        """
        if mode not in self._EDIT_MODES:
            raise BadInput(
                f"unknown edit mode {mode!r}",
                options=list(self._EDIT_MODES),
                next=(
                    f"edit(kind='{self._KIND}', id='slug', mode='find-replace', "
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
        """Delete a paragraph / region from a paragraph-block file.

        Requires a selector in ``id`` (``slug~SLUG`` or
        ``slug~Lstart-Lend``). Whole-file delete is not exposed.
        """
        slug, sel, _path_view = _parse_pt_id(str(id))
        if sel is None:
            raise BadInput(
                (
                    f"delete on {self._KIND} requires a block selector — "
                    f"id={slug!r}~SLUG"
                ),
                next=(
                    f"delete(kind='{self._KIND}', id='{slug}~SLUG') to remove "
                    "a paragraph, or use the OS to remove the whole file"
                ),
            )
        return self._put_delete(slug, sel)

    def _resolve_pt_ref(self, id: str | int) -> tuple[str, int]:
        """Coerce an id to (slug, ref_id), ingesting the file if needed."""
        slug, sel, path_view = _parse_pt_id(str(id))
        if sel is not None or path_view is not None:
            raise BadInput(
                (
                    f"{self._KIND} tag/link ops operate at file level — drop the "
                    "block selector / path view from id="
                ),
                next=f"tag(kind='{self._KIND}', id={slug!r}, add=[...])",
            )
        ref = self._ensure_ingested(slug)
        if ref is None:
            raise NotFound(
                f"{self._KIND} file {slug!r} not found under {self.root}",
                next=f"get(kind='{self._KIND}') to list every known file",
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
        """Add/remove tags on a paragraph-block file."""
        if not add and not remove:
            raise BadInput(
                f"tag(kind='{self._KIND}', id=...) requires add= or remove=",
                next=f"tag(kind='{self._KIND}', id='<slug>', add=['draft'])",
            )
        from precis.handlers._link_tag_ops import apply_tag_ops, format_link_tag_ack

        slug, ref_id = self._resolve_pt_ref(id)
        n_added, n_removed = apply_tag_ops(
            self.store, self._KIND, ref_id, tags=add, untags=remove
        )
        return Response(
            body=format_link_tag_ack(
                kind=self._KIND,
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
        """Add or remove a link from a paragraph-block file to another ref."""
        if target is None:
            raise BadInput(
                f"link(kind='{self._KIND}', id=...) requires target=",
                next=(f"link(kind='{self._KIND}', id='<slug>', target='paper:slug')"),
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
                kind=self._KIND,
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
        ref = self.store.get_ref(kind=self._KIND, id=slug)

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
        # rebuild the real file path even when several extensions map
        # to the same slug shape (e.g. plaintext: .txt vs .log).
        rel = str(path.relative_to(self.root))
        _base, ext = self._strip_ext(rel)
        new_meta = {
            "path": rel,
            "ext": ext or self._DEFAULT_EXT,
            "mtime_ns": mtime_ns,
            "mtime_iso": datetime.datetime.fromtimestamp(
                st.st_mtime, tz=datetime.UTC
            ).isoformat(),
            "sha256": sha,
            "size": st.st_size,
        }

        inserts = to_block_inserts(
            pt_blocks, embedder=self.embedder, meta_for=_plaintext_block_meta
        )

        with self.store.tx() as conn:
            corpus_id = self.store.ensure_corpus("default")
            if ref is None:
                ref = self.store.insert_ref(
                    corpus_id=corpus_id,
                    kind=self._KIND,
                    slug=slug,
                    title=title,
                    meta=new_meta,
                    conn=conn,
                )
            else:
                self.store.update_ref(ref.id, title=title, meta_patch=new_meta)

            self.store.insert_blocks(ref.id, inserts, replace=True, conn=conn)

        return self.store.get_ref(kind=self._KIND, id=slug)

    # ── render helpers ─────────────────────────────────────────────

    def _render_index(self) -> Response:
        on_disk = sorted(self._walk_files())
        seen: dict[str, str] = {}
        for path in on_disk:
            try:
                rel = str(path.relative_to(self.root))
                base, _ext = self._strip_ext(rel)
                slug = file_slug_from_path(base)
            except ValueError:
                continue
            if not is_valid_file_slug(slug):
                continue
            seen[slug] = rel

        if not seen:
            return Response(
                body=(
                    f"no {self._KIND} files found under {self.root}\n"
                    f"create one with put(kind='{self._KIND}', id='SLUG', "
                    "text='...', mode='create')"
                )
            )

        lines = [f"# {len(seen)} {self._KIND} file(s) under {self.root}"]
        max_w = max(len(s) for s in seen)
        for slug in sorted(seen):
            lines.append(f"  {slug:<{max_w}}  {seen[slug]}")
        body = "\n".join(lines)
        body += render_next_section(
            [
                (f"get(kind='{self._KIND}', id='<slug>')", "open a file"),
                (f"get(kind='{self._KIND}', id='<slug>/raw')", "full source"),
                (
                    f"search(kind='{self._KIND}', q='...', scope='<slug>')",
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
                (f"get(kind='{self._KIND}', id='{ref.slug}/raw')", "full source"),
                (
                    f"get(kind='{self._KIND}', id='{ref.slug}~SLUG')",
                    "read one paragraph by slug",
                ),
                (
                    f"search(kind='{self._KIND}', q='...', scope='{ref.slug}')",
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
                    next=f"get(kind='{self._KIND}', id='{ref.slug}~SLUG')",
                ) from exc
            block = self.store.get_block(ref.id, pos=pos)
            if block is None:
                raise NotFound(
                    f"no paragraph at ~{pos} in {ref.slug!r}",
                    next=f"get(kind='{self._KIND}', id='{ref.slug}')",
                )
        else:
            block = self.store.get_block(ref.id, slug=sel.value)
            if block is None:
                raise NotFound(
                    f"no paragraph with slug {sel.value!r} in {ref.slug!r}",
                    next=f"get(kind='{self._KIND}', id='{ref.slug}')",
                )
        handle = f"{ref.slug}~{block.slug or block.pos}"
        body = f"# {handle}\n{block.text}"
        body += render_next_section(
            [
                (
                    f"get(kind='{self._KIND}', id='{ref.slug}')",
                    "back to overview",
                ),
                (
                    (
                        f"edit(kind='{self._KIND}', id='{handle}', "
                        "text='...', mode='replace')"
                    ),
                    "edit this paragraph",
                ),
                (
                    f"delete(kind='{self._KIND}', id='{handle}')",
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
                f"invalid {self._KIND} slug: {slug!r}",
                next="slugs are lowercase a-z 0-9 hyphens, segments split by '--'",
            )
        # Look up the already-ingested ref to know which extension to
        # use. Fresh files default to ``self._DEFAULT_EXT``; re-reads
        # honour the stored extension so opening (e.g. a .log) doesn't
        # accidentally read a .txt.
        ext = self._DEFAULT_EXT
        existing = self.store.get_ref(kind=self._KIND, id=slug)
        if existing is not None:
            ext = (existing.meta or {}).get("ext") or self._DEFAULT_EXT
        else:
            # On first touch, probe disk for any registered extension
            # and prefer whichever exists. Ties fall back to the
            # default.
            rel_base = path_from_file_slug(slug, ext="").rstrip(".")
            for candidate_ext in self._EXTENSIONS:
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
                f"{self._KIND} file not found on disk: {path}",
                next=(
                    f"put(kind='{self._KIND}', id='<slug>', text='...', mode='create')"
                ),
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


def _plaintext_block_meta(pb: PlaintextBlock) -> dict[str, Any]:
    """Per-block metadata for plaintext: just the line span.

    Plaintext has no "kind" axis (every block is a paragraph), so the
    layout is deliberately thinner than markdown's ``block_meta``.
    """
    return {"line_start": pb.line_start, "line_end": pb.line_end}


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
