"""MarkdownHandler — read/write ``.md`` files under a configured root.

Thin subclass of :class:`precis.handlers.plaintext.PlaintextHandler`.
Overrides the kind-specific hooks:

- ``_KIND`` / ``_EXTENSIONS`` / ``_DEFAULT_EXT`` / ``spec`` —
  identity.
- :meth:`_parse_blocks` — drives markdown's heading / paragraph /
  code / list / table block grammar via :func:`parse_markdown`.
- :meth:`_block_meta` — stores ``kind`` (``heading`` / ``paragraph`` /
  ``code`` / ``list`` / ``table``) and ``heading_level`` per block
  so the ``/toc`` renderer can rebuild the nesting tree.
- :meth:`_derive_title` — first H1 heading, falling back to any
  heading, then to the file slug.
- :meth:`_render_view` — adds the ``/toc`` view.
- :meth:`_overview_body_extras` — shows a heading-TOC preview
  inside the overview, instead of the first-5-paragraphs default.
- :meth:`_overview_blocks_label` / :meth:`_overview_next_hints` —
  switch plaintext's ``paragraphs:`` label to ``blocks:`` and the
  hint wording to ``block`` terminology.

All the write / read / search / tag / link / ingest / address-
parsing plumbing (including Track-A line-range addressing,
path-form canonicalisation, unified write-result format, nearest-
match slug options, and the file-level ``delete`` confirm-gate)
lives in :class:`PlaintextHandler` and is inherited unchanged.
That makes every MCP-critic file-kind fix apply uniformly to
``markdown``, ``plaintext``, and ``tex`` from one source of truth.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, ClassVar

from precis.handlers._paper_toc import build_toc, render_toc
from precis.handlers.plaintext import PlaintextHandler
from precis.protocol import KindSpec
from precis.response import Response
from precis.store import Ref
from precis.utils.md_parse import MdBlock, block_meta, parse_markdown
from precis.utils.next_block import render_next_section


class MarkdownHandler(PlaintextHandler):
    """Slug-addressed read/write handler for ``.md`` / ``.markdown`` files."""

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
        views=("toc", "raw"),
        modes=("create",),
    )

    _KIND: ClassVar[str] = "markdown"
    _EXTENSIONS: ClassVar[tuple[str, ...]] = (".md", ".markdown")
    _DEFAULT_EXT: ClassVar[str] = ".md"
    _SUPPORTED_VIEWS: ClassVar[tuple[str, ...]] = ("toc", "raw")

    # ── parser hooks (override PlaintextHandler) ─────────────────────

    def _parse_blocks(self, content: str) -> list[MdBlock]:  # type: ignore[override]
        """Markdown block grammar: headings, paragraphs, fenced code,
        lists, tables. See :func:`precis.utils.md_parse.parse_markdown`.
        """
        return parse_markdown(content)

    def _block_meta(self, block: MdBlock) -> dict[str, Any]:  # type: ignore[override]
        """Store per-block metadata — the :func:`block_meta` helper in
        :mod:`precis.utils.md_parse` packs ``kind`` /
        ``heading_level`` / ``line_start`` / ``line_end`` /
        language-lang-when-fenced-code into a JSON-friendly dict."""
        return block_meta(block)

    def _block_noun(self) -> str:  # type: ignore[override]
        """``block`` instead of plaintext's ``paragraph``.

        Markdown's grammar produces headings, paragraphs, fenced
        code, lists, and tables; the generic ``block`` avoids
        mis-describing the non-paragraph kinds in error messages.
        """
        return "block"

    def _derive_title(self, blocks: Sequence[Any], *, fallback: str) -> str:
        """Title = first H1, else first heading, else the file slug.

        Overrides :meth:`PlaintextHandler._derive_title`'s default
        (first line of first paragraph) because a markdown file's
        first meaningful text is almost always a heading.
        """
        for b in blocks:
            if b.kind == "heading" and b.heading_level == 1:
                return b.text.lstrip("#").strip()
        for b in blocks:
            if b.kind == "heading":
                return b.text.lstrip("#").strip()
        return fallback

    # ── view dispatch ────────────────────────────────────────────────

    def _render_view(self, view: str, ref: Ref, *, slug: str) -> Response:  # type: ignore[override]
        """Add the ``/toc`` view on top of plaintext's ``/raw``."""
        if view == "toc":
            return self._render_toc(ref)
        return super()._render_view(view, ref, slug=slug)

    def _render_toc(self, ref: Ref) -> Response:
        """Render a hierarchical heading-TOC for this file.

        Uses the shared :func:`precis.handlers._paper_toc.build_toc`
        / :func:`render_toc` machinery (same renderer paper and
        patent overviews use) so the ``/toc`` view looks consistent
        across every slug-addressed block-bearing kind.
        """
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

    # ── overview customisation ──────────────────────────────────────

    def _overview_blocks_label(self) -> str:  # type: ignore[override]
        """``blocks:`` instead of plaintext's ``paragraphs:``.

        Markdown's block grammar is heterogeneous (headings,
        paragraphs, fenced code, lists, tables) so ``paragraphs:``
        would mis-label most of a typical file.
        """
        return "blocks:      "

    def _overview_body_extras(  # type: ignore[override]
        self, ref: Ref, blocks: Sequence[Any]
    ) -> list[str]:
        """Heading-TOC preview in place of plaintext's paragraph list.

        Renders the first ten heading entries (H1s plus their nested
        H2s) with indentation, so an agent opening a fresh file can
        pick a section to drill into without fetching ``/toc``. When
        the document has more than ten headings an ellipsis line
        points at the full ``/toc`` view.
        """
        toc = build_toc(list(blocks))
        flat: list[Any] = []
        for s in toc:
            if s.title:
                flat.append(s)
            for child in s.children:
                if child.title:
                    flat.append(child)
        if not flat:
            return []
        lines: list[str] = ["", "## Headings"]
        for entry in flat[:10]:
            indent = "  " * max(entry.level - 1, 0)
            lines.append(f"{indent}- ~{entry.start} {entry.title}")
        if len(flat) > 10:
            lines.append(f"  … and {len(flat) - 10} more (see /toc)")
        return lines

    def _overview_next_hints(  # type: ignore[override]
        self, ref: Ref
    ) -> list[tuple[str, str]]:
        """Markdown's hint set mentions ``/toc`` and ``block`` nouns."""
        return [
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

    # ── index Next: hints — ``toc`` instead of ``raw`` ──────────────

    def _render_index(self) -> Response:  # type: ignore[override]
        """Same layout as plaintext's index, but the Next: hints
        mention ``/toc`` (markdown's signature view) rather than
        ``/raw``."""
        from precis.utils.md_parse import file_slug_from_path, is_valid_file_slug

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
                    "no markdown files in workspace\n"
                    "create one with put(kind='markdown', id='SLUG', "
                    "text='# Title\\n...', mode='create')"
                )
            )

        lines = [f"# {len(seen)} markdown file(s) in workspace"]
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
