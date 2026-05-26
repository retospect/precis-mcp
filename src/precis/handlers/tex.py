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

from typing import Any, ClassVar

from precis.handlers.plaintext import PlaintextHandler
from precis.protocol import KindSpec
from precis.response import Response
from precis.store import Ref
from precis.utils.next_block import render_next_section
from precis.utils.tex_parse import TEX_SECTION_NAMES, TexBlock, parse_tex


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
    )

    _KIND: ClassVar[str] = "tex"
    _EXTENSIONS: ClassVar[tuple[str, ...]] = (".tex",)
    _DEFAULT_EXT: ClassVar[str] = ".tex"
    _SUPPORTED_VIEWS: ClassVar[tuple[str, ...]] = ("raw", "toc")

    # ── parser hooks (override PlaintextHandler) ──────────────────────

    def _parse_blocks(self, content: str) -> list[TexBlock]:  # type: ignore[override]
        return parse_tex(content)

    def _block_meta(self, block: TexBlock) -> dict[str, Any]:  # type: ignore[override]
        meta: dict[str, Any] = {
            "line_start": block.line_start,
            "line_end": block.line_end,
        }
        if block.section_level is not None:
            meta["section_level"] = block.section_level
            meta["section_title"] = block.section_title
        if block.section_path:
            # JSON-friendly: list of [level, title] pairs.
            meta["section_path"] = [list(p) for p in block.section_path]
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
        n_sections = self._toc_walk(ref, visited=visited, lines=lines, depth=0)
        if n_sections == 0:
            lines.append("(no sectioning commands found)")
        body = "\n".join(lines)
        body += render_next_section(
            [
                (f"get(kind='{self._KIND}', id='{ref.slug}/raw')", "full source"),
                (f"get(kind='{self._KIND}', id='{ref.slug}')", "overview"),
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
        blocks = self.store.list_blocks_for_ref(ref.id)
        n_emitted = 0
        # Compute the depth of the outermost section in this file so we
        # can render its hierarchy starting at the current indent.
        outer_levels = [
            (b.meta or {}).get("section_level")
            for b in blocks
            if (b.meta or {}).get("section_level") is not None
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
                handle = f"{ref.slug}~{block.slug}"
                lines.append(f"{indent}- \\{command}{{{title}}}  (`{handle}`)")
                n_emitted += 1

            # Recurse into any \input{} / \include{} found in this
            # block — both inside section blocks and in plain
            # paragraphs (preamble usage is common).
            for input_arg in meta.get("inputs", ()) or ():
                child_ref = self._resolve_input_ref(ref, input_arg)
                if child_ref is None:
                    indent = "  " * (depth + 1)
                    lines.append(
                        f"{indent}\u26a0 \\input{{{input_arg}}} \u2192 not found"
                    )
                    continue
                indent = "  " * (depth + 1)
                lines.append(
                    f"{indent}\u2937 \\input{{{input_arg}}} \u2192 {child_ref.slug}"
                )
                self._toc_walk(
                    child_ref,
                    visited=visited,
                    lines=lines,
                    depth=depth + 1,
                )

        return n_emitted

    # ── \input{} / \include{} resolver ────────────────────────────────

    def _resolve_input_ref(self, parent_ref: Ref, target: str) -> Ref | None:
        """Resolve a single ``\\input{path}`` argument to an ingested
        :class:`Ref`. Returns ``None`` if the file isn't found or
        resolves outside :attr:`root` (the latter is silently dropped
        from the TOC; the ``\\input`` line still appears in the source).

        Path resolution mirrors LaTeX:

        - Try the literal target first, then with ``.tex`` appended.
        - Resolve **relative to the parent file's directory** (not the
          ``PRECIS_ROOT``), matching how ``pdflatex`` searches.
        - Apply the same ``Path.resolve()`` + ``relative_to(self.root)``
          gate every other read/write goes through.
        """
        cleaned = target.strip()
        if not cleaned:
            return None
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
            base, _ext = self._strip_ext(str(rel))
            from precis.utils.md_parse import file_slug_from_path, is_valid_file_slug

            child_slug = file_slug_from_path(base)
            if not is_valid_file_slug(child_slug):
                continue
            return self._ensure_ingested(child_slug)

        return None
