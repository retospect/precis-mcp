"""TexHandler — read/write ``.tex`` files under a configured root.

Subclasses :class:`PlaintextHandler` and overrides:

- ``_KIND`` / ``_EXTENSIONS`` / ``_DEFAULT_EXT`` ClassVars + the
  ``spec`` — kind identity.
- :meth:`_parse_blocks` / :meth:`_block_meta` — section-aware block
  grammar. ``\\section`` / ``\\subsection`` / ``\\subsubsection`` /
  ``\\paragraph`` / ``\\part`` / ``\\chapter`` start new blocks; each
  block records its section ancestry. See
  :mod:`precis.utils.tex_parse` for the parser.
- :attr:`_SUPPORTED_VIEWS` / :meth:`_render_view` — adds the ``toc``
  view (project-wide table of contents with ``\\input{}`` recursion).

What this handler is **not**:

- Not a full LaTeX parser. No macro expansion, no environment
  grouping, no comment stripping. Source text is preserved verbatim
  so anchored edits work against the original characters.
- Not a citation-graph navigator. ``\\cite{}`` keys are opaque text;
  for citation queries use ``kind='paper'``.
- Not a multi-project composer. Each ``.tex`` file is its own ref.
  ``\\input{}`` is resolved only inside the ``/toc`` view, not at
  ingest time.

Same address grammar as plaintext (``slug``, ``slug~SLUG``, ``slug~N``,
``slug/raw``, ``slug/toc``, ``/`` for index). See ``precis-tex-help``
for recipes and ``precis-files-help`` for the shared file protocol.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from precis.errors import PrecisError, Upstream
from precis.handlers.plaintext import PlaintextHandler
from precis.protocol import KindSpec
from precis.response import Response
from precis.store import Ref
from precis.utils import handle_registry
from precis.utils.next_block import render_next_section
from precis.utils.tex_parse import TEX_SECTION_NAMES, TexChunk, parse_tex


@dataclass
class _TocIngestBudget:
    """Mutable per-``/toc``-invocation ingest budget (gr311327).

    ``_toc_walk`` calls :meth:`TexHandler.ensure_ingested` once per
    ``\\input{}``/``\\include{}`` child it discovers — on a large
    ``\\input`` tree (the reported repro has 105 directives across 92
    files) that is up to 105 synchronous parse+store(+embed) writes on
    one request thread. ``remaining`` counts down only for children
    that need REAL ingest work; a child whose on-disk mtime already
    matches its stored ``ref.meta['mtime_ns']`` is a free re-read
    (``ensure_ingested`` short-circuits before touching the store or
    the embedder) and doesn't decrement the budget. ``pending``
    collects the slugs skipped once the budget is exhausted, for the
    TOC's closing trailer.
    """

    remaining: int
    pending: list[str] = field(default_factory=list)


@dataclass
class _InputResolution:
    """Result of resolving one ``\\input{}``/``\\include{}`` argument.

    Three outcomes ``_toc_walk`` renders differently:

    - ``ref`` set: resolved + ingested (or already current) — walk
      recurses into it.
    - ``pending=True``: resolved to a real file but the ingest budget
      is exhausted — rendered as a "not yet indexed" marker.
    - neither: the target didn't resolve to a file under ``root`` at
      all — rendered as the pre-existing "not found" marker.
    """

    ref: Ref | None
    child_slug: str | None
    pending: bool = False


class TexHandler(PlaintextHandler):
    """Slug-addressed read/write handler for ``.tex`` files.

    Section-aware block grammar (see :mod:`precis.utils.tex_parse`).
    Adds the ``/toc`` view that renders an indented table of contents
    and recursively expands ``\\input{}`` / ``\\include{}`` references
    so a TOC of ``main.tex`` shows sections from every included file
    inline at their inclusion point.
    """

    spec: ClassVar[KindSpec] = KindSpec(
        kind="tex",
        title="LaTeX",
        description=(
            "Read and edit local LaTeX files (.tex) under PRECIS_ROOT. "
            "Section-aware block grammar (\\section / \\subsection / ... "
            "drive block boundaries); ``/toc`` view recursively expands "
            "\\input{} / \\include{} across files. Lazy re-ingest on "
            "stale mtime."
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
        note_like=True,
        views=("raw", "toc"),
        modes=("create",),
        # edit() is inherited unmodified from PlaintextHandler.
        edit_modes=("find-replace", "append", "insert", "replace"),
    )

    _KIND: ClassVar[str] = "tex"
    _EXTENSIONS: ClassVar[tuple[str, ...]] = (".tex",)
    _DEFAULT_EXT: ClassVar[str] = ".tex"
    _SUPPORTED_VIEWS: ClassVar[tuple[str, ...]] = ("raw", "toc")

    #: Cap on the number of REAL (non-free) ``ensure_ingested`` calls
    #: one ``/toc`` walk will perform (gr311327). Beyond the cap,
    #: children render as "not yet indexed" markers; a follow-up
    #: ``/toc`` call (or a direct ``get()``) indexes more.
    MAX_TOC_INGESTS: ClassVar[int] = 20

    # ── parser hooks (override PlaintextHandler) ──────────────────────

    def _parse_blocks(self, content: str) -> list[TexChunk]:  # type: ignore[override]
        return parse_tex(content)

    def _block_meta(self, block: TexChunk) -> dict[str, Any]:  # type: ignore[override]
        meta: dict[str, Any] = {
            "line_start": block.line_start,
            "line_end": block.line_end,
        }
        if block.section_level is not None:
            meta["section_level"] = block.section_level
            meta["section_title"] = block.section_title
        if block.section_path:
            # ``section_path`` is the column-bound key — store
            # flat title strings so ``chunks.section_path`` (TEXT[])
            # accepts them. The level information is preserved
            # alongside under ``section_path_pairs`` for any future
            # consumer that needs the nesting (rendered TOC, etc.).
            meta["section_path"] = [t for _, t in block.section_path]
            meta["section_path_pairs"] = [list(p) for p in block.section_path]
        if block.inputs:
            meta["inputs"] = list(block.inputs)
        return meta

    # ── view dispatch ─────────────────────────────────────────────────

    def _render_view(self, view: str, ref: Ref, *, slug: str) -> Response:
        if view == "toc":
            return self._render_toc(ref)
        return super()._render_view(view, ref, slug=slug)

    # ── TOC: section tree with \input{} recursion ─────────────────────

    def _render_toc(self, ref: Ref) -> Response:
        """Render an indented TOC of this file with ``\\input{}``
        children expanded inline.

        The walker keeps a ``visited`` set keyed by ref slug so a
        cycle (``a.tex`` includes ``b.tex`` which includes ``a.tex``)
        terminates with a marker rather than recursing forever.
        """
        lines: list[str] = [f"# TOC: {ref.slug}"]
        if ref.title and ref.title != ref.slug:
            lines.append(f"_{ref.title}_")
        lines.append("")
        visited: set[str] = set()
        budget = _TocIngestBudget(remaining=self.MAX_TOC_INGESTS)
        n_sections = self._toc_walk(
            ref, visited=visited, lines=lines, depth=0, budget=budget
        )
        if n_sections == 0:
            lines.append("(no sectioning commands found)")
        if budget.pending:
            lines.append("")
            lines.append(
                f"{len(budget.pending)} file(s) not yet indexed — re-run the "
                "toc view (or get() them) to index more."
            )
        handle = handle_registry.format_handle(self._KIND, ref.id)
        body = "\n".join(lines)
        body += render_next_section(
            [
                (f"get(kind='{self._KIND}', id='{ref.slug}/raw')", "full source"),
                (f"get(id='{handle}')", "overview"),
                # ``scope=`` is a FILE SLUG, not a handle — tex resolves
                # search scope via ``ensure_ingested`` (path + cite_key
                # lookup), so the ``tx42`` handle form raises NotFound.
                (
                    f"search(kind='{self._KIND}', q='...', scope='{ref.slug}')",
                    "search inside this file",
                ),
            ]
        )
        return Response(body=body)

    def _toc_walk(
        self,
        ref: Ref,
        *,
        visited: set[str],
        lines: list[str],
        depth: int,
        budget: _TocIngestBudget,
    ) -> int:
        """Recursive TOC walker. Returns the number of section entries
        emitted from this ref (not counting the ``↺`` cycle marker)."""
        slug = ref.slug or ""
        if slug in visited:
            indent = "  " * depth
            lines.append(f"{indent}↺ (cycle: {slug} already visited)")
            return 0
        visited.add(slug)

        # Pull this file's blocks in pos order. We need both their
        # section meta and their raw text (to scan for \input{} that
        # appear in non-section blocks).
        blocks = self.store.chunks.list_chunks_for_ref(ref.id)
        n_emitted = 0
        # Compute the depth of the outermost section in this file so we
        # can render its hierarchy starting at the current indent.
        outer_levels = [
            level
            for b in blocks
            if (level := (b.meta or {}).get("section_level")) is not None
        ]
        outer_min = min(outer_levels) if outer_levels else 0

        for block in blocks:
            meta = block.meta or {}
            level = meta.get("section_level")
            if level is not None:
                rel_depth = level - outer_min
                indent = "  " * (depth + rel_depth)
                title = meta.get("section_title") or ""
                command = TEX_SECTION_NAMES[level + 2]  # offset for part=-2
                handle = (
                    handle_registry.try_format(self._KIND, block.id, chunk=True)
                    or f"{ref.slug}~{block.slug}"
                )
                lines.append(f"{indent}- \\{command}{{{title}}}  (`{handle}`)")
                n_emitted += 1

            # Recurse into any \input{} / \include{} found in this
            # block — both inside section blocks and in plain
            # paragraphs (preamble usage is common).
            for input_arg in meta.get("inputs", ()) or ():
                resolution = self._resolve_input_ref(ref, input_arg, budget=budget)
                indent = "  " * (depth + 1)
                if resolution.pending:
                    lines.append(
                        f"{indent}… \\input{{{input_arg}}} → "
                        f"{resolution.child_slug} (not yet indexed)"
                    )
                    continue
                if resolution.ref is None:
                    lines.append(f"{indent}⚠ \\input{{{input_arg}}} → not found")
                    continue
                lines.append(
                    f"{indent}⤷ \\input{{{input_arg}}} → {resolution.ref.slug}"
                )
                self._toc_walk(
                    resolution.ref,
                    visited=visited,
                    lines=lines,
                    depth=depth + 1,
                    budget=budget,
                )

        return n_emitted

    # ── \input{} / \include{} resolver ────────────────────────────────

    def _is_toc_child_fresh(self, slug: str, abs_path: Path) -> bool:
        """True if *slug* is already ingested and current for *abs_path*.

        Mirrors the cheap first fast-path branch of
        :meth:`PlaintextHandler.ensure_ingested` (mtime_ns match) —
        this is the "free, doesn't count against the TOC ingest
        budget" case. A stale mtime but unchanged content (the sha256
        fallback branch inside ``ensure_ingested``) still counts
        against the budget here; that's a deliberately conservative
        simplification, not a correctness issue — it just means the
        walker may spend one extra budget slot on a touched-but-
        unchanged file.
        """
        ref = self.store.get_ref(kind=self._KIND, id=slug)
        if ref is None:
            return False
        try:
            mtime_ns = abs_path.stat().st_mtime_ns
        except OSError:
            return False
        return (ref.meta or {}).get("mtime_ns") == mtime_ns

    def _resolve_input_ref(
        self, parent_ref: Ref, target: str, *, budget: _TocIngestBudget
    ) -> _InputResolution:
        """Resolve a single ``\\input{path}`` argument.

        Returns an :class:`_InputResolution`: neither ``ref`` nor
        ``pending`` set if the file isn't found or resolves outside
        :attr:`root` (the latter is silently dropped from the TOC;
        the ``\\input`` line still appears in the source);
        ``pending=True`` if the target resolves to a real file but the
        per-walk ingest *budget* is exhausted (gr311327); ``ref`` set
        otherwise.

        Path resolution mirrors LaTeX:

        - Try the literal target first, then with ``.tex`` appended.
        - Resolve **relative to the parent file's directory** (not the
          ``PRECIS_ROOT``), matching how ``pdflatex`` searches.
        - Apply the same ``Path.resolve()`` + ``relative_to(self.root)``
          gate every other read/write goes through.
        """
        cleaned = target.strip()
        if not cleaned:
            return _InputResolution(ref=None, child_slug=None)
        parent_path = self._resolve_path(parent_ref.slug or "", must_exist=False)
        parent_dir = parent_path.parent

        candidates: list[str] = [cleaned]
        if not cleaned.lower().endswith(".tex"):
            candidates.append(cleaned + ".tex")

        for cand in candidates:
            try:
                abs_path = (parent_dir / cand).resolve()
            except OSError:
                continue
            try:
                abs_path.relative_to(self.root)
            except ValueError:
                # Resolved outside root — refuse silently in the TOC
                # walker. A future refinement could surface this as a
                # warning marker.
                continue
            if not abs_path.is_file():
                continue
            # Convert back to a slug under our root + ingest lazily.
            try:
                rel = abs_path.relative_to(self.root)
            except ValueError:
                continue
            from precis.utils.md_parse import file_slug_from_path, is_valid_file_slug

            # ``file_slug_from_path`` strips the extension itself — don't
            # pre-strip via ``_strip_ext`` first, or a stem with a further
            # ``.`` gets double-stripped and the slug can't resolve
            # (gr311326).
            child_slug = file_slug_from_path(str(rel))
            if not is_valid_file_slug(child_slug):
                continue

            if not self._is_toc_child_fresh(child_slug, abs_path):
                if budget.remaining <= 0:
                    budget.pending.append(child_slug)
                    return _InputResolution(
                        ref=None, child_slug=child_slug, pending=True
                    )
                budget.remaining -= 1

            try:
                child_ref = self.ensure_ingested(child_slug)
            except PrecisError:
                # Already a typed, agent-facing error — surface as-is.
                raise
            except Exception as exc:
                # A guarded embedder failure no longer raises here (see
                # to_chunk_inserts) — anything that still escapes is a
                # genuine ingest failure (e.g. a DB error). Wrap it as
                # ONE typed error naming the offending child rather
                # than letting the walk abort mid-tree with a raw
                # internal leak (gr311327).
                raise Upstream(
                    f"failed to index \\input child {child_slug!r} "
                    f"(from {parent_ref.slug!r}): {exc}",
                    next=(
                        f"get(kind='{self._KIND}', id='{child_slug}') to see "
                        "the file directly, or retry the toc view"
                    ),
                ) from exc
            return _InputResolution(ref=child_ref, child_slug=child_slug)

        return _InputResolution(ref=None, child_slug=None)
