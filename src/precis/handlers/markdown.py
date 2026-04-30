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

from precis.embedder import Embedder
from precis.errors import BadInput, NotFound, Unsupported
from precis.handlers._paper_toc import build_toc, render_toc
from precis.protocol import Handler, KindSpec
from precis.response import Response
from precis.store import SEMANTIC_DISTANCE_FLOOR, BlockInsert, Ref, Store
from precis.utils.md_parse import (
    MdBlock,
    file_slug_from_path,
    is_valid_file_slug,
    parse_markdown,
    path_from_file_slug,
)
from precis.utils.next_block import render_next_section
from precis.utils.search_header import format_search_headline
from precis.utils.search_merge import SearchHit, block_hits_to_search_hits

log = logging.getLogger(__name__)


_SUPPORTED_VIEWS = ("toc", "raw")
_SUPPORTED_PUT_MODES = ("append", "replace", "delete", "create")


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
        is_numeric=False,
        id_required=False,
        views=_SUPPORTED_VIEWS,
        modes=_SUPPORTED_PUT_MODES,
    )

    def __init__(
        self,
        *,
        store: Store,
        root: Path,
        embedder: Embedder | None = None,
    ) -> None:
        if not root.exists() or not root.is_dir():
            raise ValueError(
                f"markdown root {str(root)!r} does not exist or is not a directory"
            )
        self.store = store
        self.embedder = embedder
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
                f"markdown file {slug!r} not found under {self.root}",
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
            return Response(
                body=(
                    f"no markdown blocks match {q!r}\n"
                    "next: try a broader phrase or scope='<file-slug>' "
                    "to search inside a specific note"
                )
            )

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

    # ── put ────────────────────────────────────────────────────────

    def put(  # type: ignore[override]
        self,
        *,
        id: str | int | None = None,
        text: str | None = None,
        mode: str | None = None,
        **_kw: Any,
    ) -> Response:
        if mode is None or mode not in _SUPPORTED_PUT_MODES:
            raise BadInput(
                f"mode= is required, one of {list(_SUPPORTED_PUT_MODES)}",
                options=list(_SUPPORTED_PUT_MODES),
                next="put(kind='markdown', id='foo', text='...', mode='append')",
            )

        if id is None:
            raise BadInput(
                "put requires id=",
                next="put(kind='markdown', id='foo', text='...', mode='append')",
            )

        slug, sel, _path_view = _parse_md_id(str(id))

        if mode == "create":
            return self._put_create(slug, text)
        if mode == "append":
            return self._put_append(slug, text)
        if mode == "replace":
            return self._put_replace(slug, sel, text)
        if mode == "delete":
            return self._put_delete(slug, sel)

        raise Unsupported(  # pragma: no cover — defensive
            f"unhandled mode {mode!r}",
            options=list(_SUPPORTED_PUT_MODES),
        )

    # ── put helpers ────────────────────────────────────────────────

    def _put_create(self, slug: str, text: str | None) -> Response:
        path = self._resolve_path(slug, must_exist=False)
        if path.exists():
            raise BadInput(
                f"file already exists: {path}",
                next=f"put(kind='markdown', id={slug!r}, text=..., mode='replace') if you mean to edit",
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
                next=f"put(kind='markdown', id='{slug}~BLOCK', mode='delete')",
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

    # ── ingest pipeline ────────────────────────────────────────────

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
            return ref

        # Slow path — re-read and re-hash.
        content = path.read_text(encoding="utf-8")
        sha = hashlib.sha256(content.encode("utf-8")).hexdigest()

        if not force and ref is not None and meta.get("sha256") == sha:
            # Same content, just touched. Bump mtime in meta and bail.
            self.store.update_ref(ref.id, meta_patch={"mtime_ns": mtime_ns})
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

        embeddings = self._embed_blocks(md_blocks)

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

            inserts = [
                BlockInsert(
                    pos=mb.pos,
                    slug=mb.slug,
                    text=mb.text,
                    embedding=embeddings[i] if embeddings else None,
                    meta=_block_meta(mb),
                )
                for i, mb in enumerate(md_blocks)
            ]
            self.store.insert_blocks(ref.id, inserts, replace=True, conn=conn)

        # Re-fetch to pick up the patched meta + new title.
        refreshed = self.store.get_ref(kind="markdown", id=slug)
        return refreshed

    def _embed_blocks(self, blocks: list[MdBlock]) -> list[list[float]] | None:
        """Embed every block, or None if no embedder is configured.

        Mock embedder is fine for tests; production uses bge-m3. We
        embed serially — markdown files are tiny vs paper bundles.
        """
        if self.embedder is None or not blocks:
            return None
        return [self.embedder.embed_one(b.text) for b in blocks]

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
                    f"no markdown files found under {self.root}\n"
                    f"create one with put(kind='markdown', id='SLUG', text='# Title\\n...', mode='create')"
                )
            )

        lines = [f"# {len(seen)} markdown file(s) under {self.root}"]
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
                    f"put(kind='markdown', id='{handle}', text='...', mode='replace')",
                    "edit this block",
                ),
                (
                    f"put(kind='markdown', id='{handle}', mode='delete')",
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
                f"markdown file not found on disk: {path}",
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


def _block_meta(mb: MdBlock) -> dict[str, Any]:
    out: dict[str, Any] = {
        "kind": mb.kind,
        "line_start": mb.line_start,
        "line_end": mb.line_end,
    }
    if mb.heading_level is not None:
        out["heading_level"] = mb.heading_level
    if mb.meta:
        out.update(mb.meta)
    return out


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


def _replace_lines(
    path: Path, line_start: int, line_end: int, new_lines: list[str]
) -> None:
    """Replace 1-indexed inclusive ``[line_start, line_end]`` with new content.

    If ``new_lines`` is empty, the lines are deleted (and any trailing
    blank line is collapsed so we don't grow a stack of empty blanks).
    """
    raw = path.read_text(encoding="utf-8")
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
    _atomic_write(path, new_content)


def _excerpt(text: str, *, limit: int = 240) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"
