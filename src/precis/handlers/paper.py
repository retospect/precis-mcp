"""Paper handler — read-only access to pre-ingested scientific papers.

Wraps acatome-store for paper reading, search, and notes.
Requires the ``paper`` extra: ``pip install precis-mcp[paper]``.
"""

from __future__ import annotations

import logging
from typing import Any

from precis.grep import parse_grep
from precis.protocol import Handler, PrecisError

log = logging.getLogger(__name__)

_store_singleton = None


def _get_store():
    """Lazy-load acatome_store to avoid hard dependency at import time."""
    global _store_singleton
    if _store_singleton is not None:
        return _store_singleton
    try:
        from acatome_store.store import Store
        _store_singleton = Store()
        return _store_singleton
    except ImportError:
        raise PrecisError(
            "Paper support requires acatome-store.\n"
            "Install with: pip install precis-mcp[paper]"
        )


def _truncate(text: str, n: int = 100) -> str:
    return (text[:n] + "…") if len(text) > n else text


def _parse_section(raw: str) -> str:
    """Extract clean section heading from JSON section_path string."""
    import json as _json
    try:
        sp = _json.loads(raw) if raw else []
    except (ValueError, TypeError):
        sp = []
    heading = sp[0] if sp else ""
    # Normalize unicode: minus sign → hyphen, etc.
    heading = heading.replace("\u2212", "-").replace("\u2013", "-").replace("\u2014", "-")
    return heading


class PaperHandler(Handler):
    """Handler for paper: scheme — read-only with notes."""

    scheme = "paper"
    writable = False
    views = {"meta", "abstract", "summary", "toc", "chunk", "page", "fig", "cite"}
    extensions: set[str] = set()

    def read(
        self,
        path: str,
        selector: str | None,
        view: str | None,
        subview: str | None,
        query: str,
        summarize: bool,
        depth: int,
        page: int,
        **kwargs,
    ) -> str:
        store = _get_store()
        top_k = kwargs.get("top_k", 5)

        # Bare call: list all papers or grep
        if not path and not selector and not view:
            if query:
                return self._search_or_grep(store, query, top_k=top_k)
            return self._list_papers(store)

        # Search mode (scoped to a paper)
        if query:
            return self._search_or_grep(store, query, top_k=top_k)

        # Resolve paper
        if not path:
            raise PrecisError("paper identifier required (e.g. get(id='miller2023foo'))")

        paper = self._resolve_paper(store, path)
        slug = paper.get("slug", "???")

        # View dispatch
        if view == "abstract":
            return self._read_abstract(store, paper)
        elif view == "toc":
            return self._read_toc(store, paper, selector)
        elif view == "meta":
            return self._read_meta(paper)
        elif view == "summary":
            return self._read_summary(store, paper, selector)
        elif view == "cite":
            return self._read_citation(paper, subview or "bib")
        elif view == "fig":
            return self._read_figures(store, paper)
        elif view == "page":
            return self._read_page(store, paper, selector)
        elif view == "chunk":
            if selector:
                return self._read_chunks(store, paper, selector)
            return self._read_toc(store, paper)

        # Selector without view: specific chunk(s)
        if selector:
            return self._read_chunks(store, paper, selector)

        # Default: overview
        return self._read_overview(store, paper)

    def put(
        self,
        path: str,
        selector: str | None,
        text: str,
        mode: str,
        **kwargs,
    ) -> str:
        if mode != "note":
            raise PrecisError(
                f"paper: scheme is read-only (mode '{mode}' not allowed, use 'note')"
            )
        return self._write_note(path, selector, text, **kwargs)

    # ── Internal methods ────────────────────────────────────────────

    def _resolve_paper(self, store, ident: str) -> dict[str, Any]:
        """Resolve identifier (slug, DOI, or ref_id) to a paper dict."""
        paper = store.get(ident)
        if paper is None:
            raise PrecisError(
                f"Paper not found: {ident}\n"
                "Try: get(grep='...') to filter papers, or search(query='...') to search."
            )
        return paper

    def _list_papers(self, store, grep: str = "") -> str:
        papers = store.list_papers()
        if not papers:
            return "No papers in library.\nAdd papers with acatome-extract."

        # Unified grep: plain text, /regex/, or /regex/i
        if grep:
            pattern = parse_grep(grep)

            def _matches(p: dict) -> bool:
                blob = " ".join([
                    p.get("slug", ""),
                    p.get("title", ""),
                    str(p.get("authors", "")),
                    str(p.get("year", "")),
                ])
                return pattern.matches(blob)

            papers = [p for p in papers if _matches(p)]
            if not papers:
                return (
                    f"No papers matching '{grep}'.\n"
                    "Try: get(grep='...') with different keywords, or /regex/i for regex."
                )
            lines = [f"📚 {len(papers)} papers matching '{grep}'", ""]
        else:
            lines = [f"📚 {len(papers)} papers in library", ""]

        _MAX_LIST = 20
        shown = papers[:_MAX_LIST]
        for p in shown:
            slug = p.get("slug", "???")
            title = _truncate(p.get("title", ""), 80)
            year = p.get("year", "")
            lines.append(f"  {slug}  {year}  {title}")

        lines.append("")
        if len(papers) > _MAX_LIST:
            lines.append(f"  ... and {len(papers) - _MAX_LIST} more (showing first {_MAX_LIST})")
            lines.append("")
            lines.append("To find specific papers:")
            lines.append("  search(query='CO2 capture')  — semantic search")
            lines.append("  get(grep='2024')             — filter by title/slug/author/year")
            lines.append("  get(grep='/MOF.*capture/i')  — regex filter")
            lines.append("")
        lines.append("Next: get(id='<slug>') for overview, "
                      "get(id='<slug>/toc') for structure")
        return "\n".join(lines)

    def _read_overview(self, store, paper: dict) -> str:
        slug = paper.get("slug", "???")
        title = paper.get("title", "")
        authors = paper.get("authors", "")
        year = paper.get("year", "")
        journal = paper.get("journal", "")
        doi = paper.get("doi", "")
        ref_id = paper.get("ref_id") or paper.get("id")

        # Get abstract from blocks
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
        lines.append("Next:")
        lines.append(f"  get(id='{slug}/toc')  — structure")
        lines.append(f"  get(id='{slug}#0..10')  — first 10 chunks")
        lines.append(f"  get(id='{slug}/cite/bib')  — BibTeX citation")
        lines.append(f"  get(id='{slug}/summary')  — paper summary")
        lines.append(f"  Cite in docs: [@{slug}]")
        return "\n".join(lines)

    def _read_abstract(self, store, paper: dict) -> str:
        slug = paper.get("slug", "???")
        blocks = store.get_blocks(slug, block_type="abstract")
        if not blocks:
            return f"No abstract available for {slug}"
        return blocks[0].get("text", "")

    def _read_meta(self, paper: dict) -> str:
        lines = []
        for key in ("slug", "title", "authors", "year", "journal", "doi",
                     "volume", "pages", "issn"):
            val = paper.get(key, "")
            if val:
                lines.append(f"  {key}: {val}")
        ref_id = paper.get("ref_id") or paper.get("id")
        if ref_id:
            lines.append(f"  ref_id: {ref_id}")
        retracted = paper.get("retracted", False)
        if retracted:
            lines.append(f"  ⚠ RETRACTED: {paper.get('retraction_note', '')}")
        return "\n".join(lines)

    # Max blocks before switching to overview TOC
    _TOC_OVERVIEW_THRESHOLD = 50
    # Max blocks per section before splitting into sub-ranges
    _MAX_SECTION_BLOCKS = 60

    def _read_toc(self, store, paper: dict, selector: str | None = None) -> str:
        slug = paper.get("slug", "???")
        toc = store.get_toc(slug)
        if not toc:
            return f"No blocks found for {slug}"

        # If selector given, show detailed TOC for that range only
        if selector:
            return self._read_toc_range(slug, toc, selector)

        # Small paper: flat detailed TOC
        if len(toc) <= self._TOC_OVERVIEW_THRESHOLD:
            return self._read_toc_flat(slug, toc)

        # Large paper: section-based overview
        return self._read_toc_overview(slug, toc)

    def _read_toc_flat(self, slug: str, toc: list[dict]) -> str:
        """Flat detailed TOC with grouped section headers."""
        return self._format_grouped_toc(
            slug, toc, f"📄 {slug}  ({len(toc)} blocks)"
        )

    # Tiny sections get merged into the previous group
    _MERGE_THRESHOLD = 3

    def _read_toc_overview(self, slug: str, toc: list[dict]) -> str:
        """Section-based overview TOC for large papers."""
        import json as _json

        # Group consecutive blocks by section heading
        raw_groups: list[dict] = []  # {heading, start, end, previews}
        current: dict | None = None

        for entry in toc:
            sp_raw = entry.get("section_path", "")
            try:
                sp_list = _json.loads(sp_raw) if sp_raw else []
            except (ValueError, TypeError):
                sp_list = []
            heading = sp_list[0] if sp_list else ""
            idx = entry.get("block_index", 0)
            preview = entry.get("summary") or entry.get("preview", "")

            if current is None or heading != current["heading"]:
                current = {
                    "heading": heading,
                    "headings": [heading] if heading else [],
                    "start": idx, "end": idx,
                    "previews": [],
                }
                raw_groups.append(current)
            current["end"] = idx
            if preview:
                current["previews"].append(preview)

        # Merge tiny sections into the previous group
        merged: list[dict] = []
        for g in raw_groups:
            size = g["end"] - g["start"] + 1
            if merged and size <= self._MERGE_THRESHOLD:
                prev = merged[-1]
                prev["end"] = g["end"]
                if g["heading"] and g["heading"] not in prev["headings"]:
                    prev["headings"].append(g["heading"])
                prev["previews"].extend(g["previews"])
            else:
                merged.append(g)

        # Split oversized groups into sub-ranges
        final_groups: list[dict] = []
        for g in merged:
            size = g["end"] - g["start"] + 1
            if size <= self._MAX_SECTION_BLOCKS:
                final_groups.append(g)
            else:
                sub_entries = [e for e in toc
                               if g["start"] <= e.get("block_index", 0) <= g["end"]]
                for i in range(0, len(sub_entries), self._MAX_SECTION_BLOCKS):
                    chunk = sub_entries[i:i + self._MAX_SECTION_BLOCKS]
                    s_idx = chunk[0].get("block_index", 0)
                    e_idx = chunk[-1].get("block_index", 0)
                    previews = [
                        e.get("summary") or e.get("preview", "")
                        for e in chunk
                        if e.get("summary") or e.get("preview", "")
                    ]
                    final_groups.append({
                        "heading": g["heading"],
                        "headings": g.get("headings", []),
                        "start": s_idx,
                        "end": e_idx,
                        "previews": previews,
                    })

        lines = [f"📄 {slug}  ({len(toc)} blocks, {len(final_groups)} sections)", ""]
        for g in final_groups:
            size = g["end"] - g["start"] + 1
            heading = g["heading"] or "(untitled)"
            # Pick first preview that differs from the heading
            snippet = ""
            heading_lower = heading.lower()
            for p in g.get("previews", []):
                if p.lower().strip() != heading_lower.strip():
                    snippet = _truncate(p, 80)
                    break
            line = f"  #{g['start']}..{g['end']}  ({size})  {_truncate(heading, 60)}"
            if snippet:
                line += f"  — {snippet}"
            lines.append(line)

        lines.append("")
        lines.append(
            f"Next: get(id='{slug}#0..{min(60, len(toc)-1)}/toc') "
            f"to drill into a range"
        )
        return "\n".join(lines)

    def _read_toc_range(self, slug: str, toc: list[dict], selector: str) -> str:
        """Detailed TOC for a block range (drill-down)."""
        try:
            if ".." in selector:
                parts = selector.split("..")
                start = int(parts[0])
                end = int(parts[1]) if parts[1] else start + 60
            else:
                start = int(selector)
                end = start + 60
        except ValueError:
            raise PrecisError(f"Invalid range: {selector}\nUse #N..M/toc")

        filtered = [e for e in toc
                    if start <= e.get("block_index", 0) <= end]
        if not filtered:
            return f"No blocks in #{start}..{end} for {slug}"

        header = f"📄 {slug}  #{start}..{end}  ({len(filtered)} blocks)"
        result = self._format_grouped_toc(slug, filtered, header)

        # Pagination hints
        last_idx = filtered[-1].get("block_index", end)
        max_idx = toc[-1].get("block_index", 0)
        if last_idx < max_idx:
            next_start = last_idx + 1
            result += (
                f"\nNext: get(id='{slug}#{next_start}..{min(next_start+60, max_idx)}/toc') "
                f"for next section"
            )
        return result

    def _format_grouped_toc(
        self, slug: str, entries: list[dict], header: str
    ) -> str:
        """Format TOC entries grouped by section heading."""
        lines = [header, ""]
        current_section = None
        has_summaries = False

        for entry in entries:
            idx = entry.get("block_index", "?")
            kind = entry.get("block_type", "text")
            preview = entry.get("summary") or entry.get("preview", "")
            section = _parse_section(entry.get("section_path", ""))
            has_summary = entry.get("has_summary", False)
            if has_summary:
                has_summaries = True

            # Emit section header when it changes
            if section != current_section:
                current_section = section
                if section:
                    # Find the block_index of the section_header entry
                    lines.append(f"  #{idx}  §{section}")
                    # If this entry IS the section header, skip the block line
                    if kind == "section_header":
                        continue

            # Block line: indented under section
            mark = "✦" if has_summary else " "
            type_tag = f"  [{kind}]" if kind != "text" else ""
            snippet = f"  {_truncate(preview, 80)}" if preview else ""
            lines.append(f"    #{idx}{mark}{type_tag}{snippet}")

        lines.append("")
        lines.append(f"Read: get(id='{slug}#N') for full chunk text")
        if has_summaries:
            lines.append(f"✦ = summary available: get(id='{slug}#N/summary')")
        return "\n".join(lines)

    def _read_summary(self, store, paper: dict, selector: str | None) -> str:
        slug = paper.get("slug", "???")
        if selector:
            # Block-level summary
            try:
                idx = int(selector)
            except ValueError:
                raise PrecisError(f"Invalid chunk index: {selector}")
            blocks = store.get_blocks(slug, block_type="text")
            target = [b for b in blocks if b.get("block_index") == idx]
            if not target:
                raise PrecisError(f"Block #{idx} not found in {slug}")
            summary = target[0].get("summary", "")
            return summary or f"No enrichment summary for block #{idx}"
        # Paper-level summary
        blocks = store.get_blocks(slug, block_type="paper_summary")
        if blocks:
            return blocks[0].get("text", "")
        return f"No paper-level summary for {slug}"

    def _read_citation(self, paper: dict, style: str) -> str:
        slug = paper.get("slug", "???")
        title = paper.get("title", "")
        authors = paper.get("authors", "")
        year = paper.get("year", "")
        journal = paper.get("journal", "")
        doi = paper.get("doi", "")

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

    def _read_chunks(self, store, paper: dict, selector: str) -> str:
        slug = paper.get("slug", "???")
        # Parse selector: single index, range, or open range
        try:
            if ".." in selector:
                parts = selector.split("..")
                start = int(parts[0])
                end = int(parts[1]) if parts[1] else start + 10
            else:
                start = int(selector)
                end = start + 1
        except ValueError:
            raise PrecisError(f"Invalid chunk selector: {selector}\nUse #N, #N..M, or #N..")

        # Fetch all text blocks and slice
        all_blocks = store.get_blocks(slug, block_type="text")
        blocks = [b for b in all_blocks
                  if start <= (b.get("block_index", 0)) < end]

        if not blocks:
            return f"No blocks in range #{start}..{end} for {slug}"

        lines = []
        for block in blocks:
            idx = block.get("block_index", "?")
            kind = block.get("block_type", "text")
            text = block.get("text", "")
            page = block.get("page", "")
            lines.append(f">> {slug} #{idx}  p{page}")
            lines.append(text)
            lines.append("")

        # Hints
        if end - start >= 10:
            lines.append(f"Next: get(id='{slug}#{end}..') for more")
        lines.append(f"Cite in docs: [@{slug}]")
        return "\n".join(lines)

    def _read_figures(self, store, paper: dict) -> str:
        slug = paper.get("slug", "???")
        figs = store.get_blocks(slug, block_type="figure")
        if not figs:
            return f"No figures found for {slug}"
        lines = [f"📊 {slug} figures ({len(figs)})", ""]
        for fig in figs:
            idx = fig.get("block_index", "?")
            page = fig.get("page", "")
            caption = fig.get("text", "")
            lines.append(f"  #{idx}  p{page}  {_truncate(caption, 100)}")
        return "\n".join(lines)

    def _read_page(self, store, paper: dict, selector: str | None) -> str:
        slug = paper.get("slug", "???")
        if not selector:
            raise PrecisError(f"Page number required: get(id='{slug}#3/page')")
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
            lines.append(f">> {slug} #{idx}  [{kind}]")
            lines.append(text)
            lines.append("")
        return "\n".join(lines)

    def _search_or_grep(self, store, query: str, top_k: int = 10) -> str:
        """Try semantic search; fall back to keyword grep on paper list."""
        try:
            return self._search(store, query, top_k=top_k)
        except (ImportError, ModuleNotFoundError):
            log.info("Semantic search unavailable, falling back to keyword grep")
            return self._list_papers(store, grep=query)

    def _search(self, store, query: str, top_k: int = 10) -> str:
        hits = store.search_text(query, top_k=top_k)
        if not hits:
            return f"No results for: {query}"
        lines = [f"🔍 {len(hits)} results for: {query}", ""]
        for hit in hits:
            text = hit.get("text", "")
            distance = hit.get("distance", 0)
            meta = hit.get("metadata", {})
            paper_info = hit.get("paper", {})
            slug = paper_info.get("slug", meta.get("slug", "???"))
            block_idx = meta.get("block_index", "?")
            # Best snippet: summary > truncated text
            summary = hit.get("summary", "")
            snippet = summary or _truncate(text, 100)
            lines.append(f"  {slug}#{block_idx}  ({distance:.2f})  {snippet}")
        lines.append("")
        # Diverse hints: single chunk, batch, toc
        seen = []
        for hit in hits[:3]:
            pi = hit.get("paper", {})
            s = pi.get("slug", hit.get("metadata", {}).get("slug"))
            bi = hit.get("metadata", {}).get("block_index")
            if s and bi is not None and s not in [x[0] for x in seen]:
                seen.append((s, bi))
        lines.append("Next:")
        if seen:
            s0, b0 = seen[0]
            lines.append(f"  get(id='{s0}#{b0}')  — read this chunk")
            if len(seen) > 1:
                batch = ",".join(f"{s}#{b}" for s, b in seen)
                lines.append(f"  get(id='{batch}')  — batch read")
            lines.append(f"  get(id='{s0}/toc')  — paper structure")
        # Cite hints for all unique slugs in results
        cite_slugs = list(dict.fromkeys(
            hit.get("paper", {}).get("slug", hit.get("metadata", {}).get("slug", ""))
            for hit in hits
        ))
        cite_slugs = [s for s in cite_slugs if s]
        if cite_slugs:
            cites = ", ".join(f"[@{s}]" for s in cite_slugs[:5])
            lines.append(f"  Cite in docs: {cites}")
        return "\n".join(lines)

    def _write_note(
        self,
        path: str,
        selector: str | None,
        text: str,
        **kwargs,
    ) -> str:
        if not text:
            raise PrecisError("text required for note")
        store = _get_store()
        paper = self._resolve_paper(store, path)
        slug = paper.get("slug", "???")
        ref_id = paper.get("ref_id") or paper.get("id")

        title = kwargs.get("title", "")
        tags = kwargs.get("tags", [])

        if selector:
            # Block-level note — need to resolve block_node_id
            try:
                block_idx = int(selector)
            except ValueError:
                raise PrecisError(f"Invalid block index for note: {selector}")
            blocks = store.get_blocks(slug, block_type="text")
            target = [b for b in blocks if b.get("block_index") == block_idx]
            if not target:
                raise PrecisError(f"Block #{block_idx} not found in {slug}")
            block_node_id = target[0].get("node_id")
            note_id = store.add_note(
                text,
                ref_id=ref_id,
                block_node_id=block_node_id,
                title=title or None,
                tags=tags or None,
            )
            return f"📝 Note #{note_id} on {slug}#{block_idx}\n{text}"
        else:
            # Paper-level note
            note_id = store.add_note(
                text,
                ref_id=ref_id,
                title=title or None,
                tags=tags or None,
            )
            return f"📝 Note #{note_id} on {slug}\n{text}"
