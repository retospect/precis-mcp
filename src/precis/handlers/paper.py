"""Paper handler — read-only access to pre-ingested scientific papers.

Extends RefHandler with paper-specific views: /abstract, /cite, /fig, /page.
Requires the ``paper`` extra: ``pip install precis-mcp[paper]``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from precis.handlers._ref_base import RefHandler, _get_store, _truncate
from precis.protocol import PrecisError

log = logging.getLogger(__name__)


class PaperHandler(RefHandler):
    """Handler for paper: scheme — read-only with notes.

    Extends RefHandler with paper-specific views:
      /abstract, /cite (bib/ris/acs), /fig, /page
    """

    scheme = "paper"
    writable = False
    corpus_id = "papers"
    views = {"meta", "abstract", "summary", "toc", "chunk", "page", "fig", "cite", "links"}
    extensions: set[str] = set()

    _ref_noun = "paper"
    _ref_emoji = "📄"

    # ── Subclass hooks ───────────────────────────────────────────────

    def _dispatch_view(
        self, store, ref: dict, view: str | None, subview: str | None,
        selector: str | None,
    ) -> str | None:
        if view == "abstract":
            return self._read_abstract(store, ref)
        elif view == "cite":
            return self._read_citation(ref, subview or "bib")
        elif view == "fig":
            return self._read_figures(store, ref, subview)
        elif view == "page":
            return self._read_page(store, ref, selector)
        return None

    def _read_overview(self, store, ref: dict) -> str:
        slug = ref.get("slug", "???")
        title = ref.get("title", "")
        authors = ref.get("authors", "")
        year = ref.get("year", "")
        journal = ref.get("journal", "")
        doi = ref.get("doi", "")

        abstract_blocks = store.get_blocks(slug, block_type="abstract")
        abstract = abstract_blocks[0]["text"] if abstract_blocks else ""

        all_blocks = store.get_blocks(slug)
        n_blocks = len(all_blocks)
        page_count = max((b.get("page") or 0) for b in all_blocks) if all_blocks else 0

        lines = [f"📄 {slug}"]
        lines.append(f"  {title}")
        if authors:
            lines.append(f"  {authors}")
        if journal or year:
            lines.append(f"  {journal} ({year})" if journal else f"  ({year})")
        if doi:
            lines.append(f"  doi:{doi}")
        lines.append(f"  {n_blocks} blocks, {page_count} pages")
        if abstract:
            lines.append("")
            lines.append(abstract[:500])
        lines.append("")
        # Link count hint
        try:
            link_counts = store.get_link_count(slug)
            if link_counts:
                total = sum(link_counts.values())
                lines.append(f"  {total} links")
        except Exception:
            pass

        lines.append("")
        lines.append("Next:")
        lines.append(f"  get(id='{slug}/toc')  — structure")
        lines.append(f"  get(id='{slug}~0..10')  — first 10 chunks")
        lines.append(f"  get(id='{slug}/cite/bib')  — BibTeX citation")
        lines.append(f"  get(id='{slug}/summary')  — paper summary")
        lines.append(f"  get(id='{slug}/links')  — links graph")
        lines.append(f"  Cite in docs: [@{slug}]")
        return "\n".join(lines)

    def _read_meta(self, ref: dict) -> str:
        lines = []
        for key in ("slug", "title", "authors", "year", "journal", "doi",
                     "volume", "pages", "issn"):
            val = ref.get(key, "")
            if val:
                lines.append(f"  {key}: {val}")
        ref_id = ref.get("ref_id") or ref.get("id")
        if ref_id:
            lines.append(f"  ref_id: {ref_id}")
        retracted = ref.get("retracted", False)
        if retracted:
            lines.append(f"  ⚠ RETRACTED: {ref.get('retraction_note', '')}")
        return "\n".join(lines)

    def _list_header(self, count: int, grep: str = "") -> str:
        if grep:
            return f"📚 {count} papers matching '{grep}'"
        return f"� {count} papers in library"

    def _list_entry(self, ref: dict) -> str:
        slug = ref.get("slug", "???")
        title = _truncate(ref.get("title", ""), 80)
        year = ref.get("year", "")
        return f"  {slug}  {year}  {title}"

    def _overview_hints(self, slug: str, ref: dict) -> list[str]:
        return [
            f"get(id='{slug}/cite/bib')  — BibTeX citation",
            f"Cite in docs: [@{slug}]",
        ]

    # ── Paper-specific views ─────────────────────────────────────────

    def _read_abstract(self, store, ref: dict) -> str:
        slug = ref.get("slug", "???")
        blocks = store.get_blocks(slug, block_type="abstract")
        if not blocks:
            return f"No abstract available for {slug}"
        return blocks[0].get("text", "")

    def _read_citation(self, ref: dict, style: str) -> str:
        slug = ref.get("slug", "???")
        title = ref.get("title", "")
        authors = ref.get("authors", "")
        year = ref.get("year", "")
        journal = ref.get("journal", "")
        doi = ref.get("doi", "")

        if style == "bib":
            entry = f"@article{{{slug},\n"
            if title:
                entry += f"  title = {{{title}}},\n"
            if authors:
                entry += f"  author = {{{authors}}},\n"
            if year:
                entry += f"  year = {{{year}}},\n"
            if journal:
                entry += f"  journal = {{{journal}}},\n"
            if doi:
                entry += f"  doi = {{{doi}}},\n"
            entry += "}\n"
            return entry
        elif style == "ris":
            lines = ["TY  - JOUR"]
            if title:
                lines.append(f"TI  - {title}")
            if authors:
                for a in authors.split(";"):
                    lines.append(f"AU  - {a.strip()}")
            if year:
                lines.append(f"PY  - {year}")
            if journal:
                lines.append(f"JO  - {journal}")
            if doi:
                lines.append(f"DO  - {doi}")
            lines.append("ER  - ")
            return "\n".join(lines)
        else:
            # ACS-style inline
            if authors and year:
                first_author = authors.split(",")[0].split(";")[0].strip()
                return f"{first_author} et al., {journal} {year}"
            return slug

    # ── Figures ───────────────────────────────────────────────────────

    _FIGURES_DIR = "figures"

    def _read_figures(self, store, ref: dict, subview: str | None = None) -> str:
        """Dispatch figure views.

        subview forms:
            None          → list all figures
            "3"           → overview of figure 3 (legend + hints)
            "3/legend"    → caption/legend text only
            "3/image"     → base64-encoded image
            "3/image/export" → export to ./figures/<slug>_fig<N>.<ext>
        """
        slug = ref.get("slug", "???")

        if not subview:
            return self._list_figures(store, slug)

        parts = subview.split("/")
        try:
            fig_num = int(parts[0])
        except ValueError:
            raise PrecisError(
                f"Invalid figure number: {parts[0]}\n"
                f"Use: get(id='{slug}/fig') to list figures"
            )

        aspect = "/".join(parts[1:]) if len(parts) > 1 else ""

        if aspect == "":
            return self._figure_overview(store, slug, fig_num)
        elif aspect == "legend":
            return self._figure_legend(store, slug, fig_num)
        elif aspect == "image":
            return self._figure_image(store, slug, fig_num)
        elif aspect == "image/export":
            return self._figure_export(store, slug, fig_num)
        else:
            raise PrecisError(
                f"Unknown figure aspect: {aspect}\n"
                f"Use: /fig/N, /fig/N/legend, /fig/N/image, /fig/N/image/export"
            )

    def _list_figures(self, store, slug: str) -> str:
        figs = store.get_figures(slug)
        if not figs:
            return f"No figures found for {slug}"
        lines = [f"📊 {slug} — {len(figs)} figure(s)", ""]
        for fig in figs:
            n = fig["fig_num"]
            page = fig.get("page", "")
            caption = _truncate(fig.get("caption", ""), 100)
            lines.append(f"  fig {n}  p{page}  {caption}")
        lines.append("")
        lines.append("Next:")
        lines.append(f"  get(id='{slug}/fig/1')              — overview")
        lines.append(f"  get(id='{slug}/fig/1/legend')       — caption text")
        lines.append(f"  get(id='{slug}/fig/1/image')        — encoded image")
        lines.append(f"  get(id='{slug}/fig/1/image/export') — save to ./figures/")
        return "\n".join(lines)

    def _figure_overview(self, store, slug: str, fig_num: int) -> str:
        figs = store.get_figures(slug)
        fig = next((f for f in figs if f["fig_num"] == fig_num), None)
        if not fig:
            return self._fig_not_found(slug, fig_num, figs)
        caption = fig.get("caption", "")
        page = fig.get("page", "")
        lines = [
            f"📊 {slug} fig {fig_num}  (page {page})",
            "",
            caption or "[no caption]",
            "",
            "Next:",
            f"  get(id='{slug}/fig/{fig_num}/legend')       — caption text",
            f"  get(id='{slug}/fig/{fig_num}/image')        — encoded image",
            f"  get(id='{slug}/fig/{fig_num}/image/export') — save to ./figures/",
        ]
        return "\n".join(lines)

    def _figure_legend(self, store, slug: str, fig_num: int) -> str:
        figs = store.get_figures(slug)
        fig = next((f for f in figs if f["fig_num"] == fig_num), None)
        if not fig:
            return self._fig_not_found(slug, fig_num, figs)
        caption = fig.get("caption", "")
        return caption if caption else f"[no caption for {slug} fig {fig_num}]"

    def _figure_image(self, store, slug: str, fig_num: int) -> str:
        result = store.get_figure_image(slug, fig_num)
        if not result:
            return (
                f"No image data for {slug} fig {fig_num}.\n"
                f"The figure may not have an embedded image in the bundle.\n"
                f"Try: get(id='{slug}/fig') to list available figures."
            )
        import base64
        b64 = base64.b64encode(result["image_bytes"]).decode("ascii")
        mime = "image/png" if result["image_ext"] == ".png" else "image/jpeg"
        lines = [
            f"📊 {slug} fig {fig_num}  ({len(result['image_bytes'])} bytes, {mime})",
            "",
            f"data:{mime};base64,{b64}",
            "",
            f"Next: get(id='{slug}/fig/{fig_num}/image/export') — save to file",
        ]
        return "\n".join(lines)

    def _figure_export(self, store, slug: str, fig_num: int) -> str:
        result = store.get_figure_image(slug, fig_num)
        if not result:
            return (
                f"No image data for {slug} fig {fig_num}.\n"
                f"Try: get(id='{slug}/fig') to list available figures."
            )
        out_dir = Path(self._FIGURES_DIR)
        out_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{slug}_fig{fig_num}{result['image_ext']}"
        out_path = out_dir / filename
        out_path.write_bytes(result["image_bytes"])
        return (
            f"✓ Exported {slug} fig {fig_num} → {out_path}\n"
            f"  {len(result['image_bytes'])} bytes, {result['image_ext']}\n"
            f"  Caption: {_truncate(result.get('caption', ''), 120)}"
        )

    @staticmethod
    def _fig_not_found(slug: str, fig_num: int, figs: list) -> str:
        available = ", ".join(str(f["fig_num"]) for f in figs)
        return (
            f"Figure {fig_num} not found for {slug}.\n"
            f"Available: {available or 'none'}\n"
            f"Use: get(id='{slug}/fig') to list figures."
        )

    def _read_page(self, store, ref: dict, selector: str | None) -> str:
        slug = ref.get("slug", "???")
        if not selector:
            raise PrecisError(f"Page number required: get(id='{slug}~3/page')")
        try:
            page_num = int(selector)
        except ValueError:
            raise PrecisError(f"Invalid page number: {selector}")
        all_blocks = store.get_blocks(slug)
        page_blocks = [b for b in all_blocks if b.get("page") == page_num]
        if not page_blocks:
            return f"No blocks on page {page_num} of {slug}"
        lines = [f"📄 {slug}  page {page_num}  ({len(page_blocks)} blocks)", ""]
        for block in page_blocks:
            idx = block.get("block_index", "?")
            kind = block.get("block_type", "text")
            text = block.get("text", "")
            lines.append(f">> {slug} ~{idx}  [{kind}]")
            lines.append(text)
            lines.append("")
        return "\n".join(lines)

