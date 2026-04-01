"""RefHandler — base handler for corpus-backed refs.

Provides generic read operations (TOC, chunks, links, summary, search,
list, notes) that work for any corpus type.  Subclasses override:

  * ``_read_overview()`` — corpus-specific overview formatting
  * ``_read_meta()``     — corpus-specific metadata display
  * ``_dispatch_view()`` — hook for custom views (e.g. /cite, /fig, /state)
  * ``_overview_hints()``— extra Next: lines in overview
  * ``_list_header()``   — header line for list output
  * ``_list_entry()``    — format a single list entry

See PaperHandler and TodoHandler for concrete examples.
"""

from __future__ import annotations

import json as _json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from precis.grep import parse_grep
from precis.protocol import Handler, PrecisError

log = logging.getLogger(__name__)


def _get_store():
    """Lazy-load acatome_store to avoid hard dependency at import time."""
    from precis._store import get_store
    return get_store()


def _truncate(text: str, n: int = 100) -> str:
    return (text[:n] + "…") if len(text) > n else text


def _parse_section(raw: str) -> str:
    """Extract clean section heading from JSON section_path string."""
    try:
        sp = _json.loads(raw) if raw else []
    except (ValueError, TypeError):
        sp = []
    heading = sp[0] if sp else ""
    heading = heading.replace("\u2212", "-").replace("\u2013", "-").replace("\u2014", "-")
    return heading


def _parse_date_value(value: str) -> datetime | None:
    """Parse a date keyword or ISO date string into a datetime.

    Supports: today, yesterday, this-week, this-month, ISO date (YYYY-MM-DD).
    Returns None if the value isn't recognized as a date.
    """
    v = value.strip().lower()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if v == "today":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if v == "yesterday":
        return (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    if v == "this-week":
        monday = now - timedelta(days=now.weekday())
        return monday.replace(hour=0, minute=0, second=0, microsecond=0)
    if v == "this-month":
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    try:
        return datetime.strptime(v, "%Y-%m-%d")
    except ValueError:
        return None


def _parse_year_value(value: str) -> tuple[int | None, int | None]:
    """Parse a year or year range string.

    Examples: '2024' → (2024,2024), '2020-2024' → (2020,2024), '2020-' → (2020,None).
    Returns (None, None) on invalid input.
    """
    value = value.strip()
    if not value:
        return (None, None)
    if "-" in value:
        parts = value.split("-", 1)
        try:
            lo = int(parts[0])
        except ValueError:
            return (None, None)
        hi_str = parts[1].strip()
        if not hi_str:
            return (lo, None)
        try:
            return (lo, int(hi_str))
        except ValueError:
            return (None, None)
    try:
        y = int(value)
        return (y, y)
    except ValueError:
        return (None, None)


_FILTER_PREFIXES = {"ingested", "year", "tag"}


def _parse_filters(grep: str) -> dict[str, str]:
    """Parse structured prefix filters from a grep string.

    Recognized prefixes: ingested:, year:, tag:.
    Remaining text becomes the 'grep' key.
    """
    result: dict[str, str] = {}
    remaining: list[str] = []
    for token in grep.split():
        if ":" in token:
            prefix, val = token.split(":", 1)
            if prefix in _FILTER_PREFIXES and val:
                result[prefix] = val
                continue
        remaining.append(token)
    result["grep"] = " ".join(remaining)
    return result


def _relative_date(dt: datetime | None) -> str:
    """Format a datetime as a relative string (today, yesterday, 3d ago, etc.)."""
    if dt is None:
        return ""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    delta = now - dt
    days = delta.days
    if days == 0:
        return "today"
    if days == 1:
        return "yesterday"
    if days < 7:
        return f"{days}d ago"
    if days < 30:
        weeks = days // 7
        return f"{weeks}w ago"
    if days < 365:
        months = days // 30
        return f"{months}mo ago"
    return dt.strftime("%Y-%m-%d")


# ─────────────────────────────────────────────────────────────────────
# RefHandler base
# ─────────────────────────────────────────────────────────────────────


class RefHandler(Handler):
    """Base handler for corpus-backed refs (papers, todos, wiki, etc.).

    Subclasses must set ``scheme`` and typically override:
      - ``_read_overview()``
      - ``_read_meta()``
      - ``_dispatch_view()`` for custom views
      - ``_overview_hints()`` for extra Next: lines
    """

    scheme: str = ""
    writable: bool = False
    corpus_id: str = ""

    # Subclass display config
    _ref_noun: str = "ref"     # "paper", "todo", "wiki page"
    _ref_emoji: str = "📄"
    _max_list: int = 20

    # Base views provided by RefHandler
    _views_base: set[str] = {"meta", "summary", "toc", "chunk", "links"}

    # Max blocks before switching to overview TOC
    _TOC_OVERVIEW_THRESHOLD = 50
    # Max blocks per section before splitting into sub-ranges
    _MAX_SECTION_BLOCKS = 60
    # Tiny sections get merged into the previous group
    _MERGE_THRESHOLD = 3

    # ── Main dispatch ────────────────────────────────────────────────

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

        # Bare call: list all refs or grep
        if not path and not selector and not view:
            if query:
                return self._search_or_grep(store, query, top_k=top_k)
            return self._list_refs(store)

        # Search mode (scoped to a ref)
        if query:
            return self._search_or_grep(store, query, top_k=top_k)

        # Resolve ref
        if not path:
            raise PrecisError(
                f"{self._ref_noun} identifier required "
                f"(e.g. get(id='<slug>'))"
            )

        ref = self._resolve_ref(store, path)

        # Base view dispatch
        if view == "toc":
            return self._read_toc(store, ref, selector)
        elif view == "summary":
            return self._read_summary(store, ref, selector)
        elif view == "links":
            return self._read_links(store, ref, selector)
        elif view == "meta":
            return self._read_meta(ref)
        elif view == "chunk":
            if selector:
                return self._read_chunks(store, ref, selector)
            return self._read_toc(store, ref)

        # Subclass view dispatch
        result = self._dispatch_view(store, ref, view, subview, selector)
        if result is not None:
            return result

        # Unknown view
        if view:
            raise PrecisError(
                f"Unknown view: /{view}\n"
                f"Available: {', '.join(sorted(self.views))}"
            )

        # Selector without view: specific chunk(s)
        if selector:
            return self._read_chunks(store, ref, selector)

        # Default: overview
        return self._read_overview(store, ref)

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
                f"{self.scheme}: scheme is read-only (mode '{mode}' not allowed).\n"
                f"Use put(mode='note') to annotate, or put(note=...) / put(link=...)."
            )
        return self._write_note(path, selector, text, **kwargs)

    # ── Subclass hooks ───────────────────────────────────────────────

    def _dispatch_view(
        self, store, ref: dict, view: str | None, subview: str | None,
        selector: str | None,
    ) -> str | None:
        """Override in subclass to handle custom views.

        Return a string to handle the view, or None to fall through.
        """
        return None

    def _read_overview(self, store, ref: dict) -> str:
        """Override in subclass for corpus-specific overview formatting."""
        slug = ref.get("slug", "???")
        title = ref.get("title", "")
        all_blocks = store.get_blocks(slug)
        n_blocks = len(all_blocks)

        lines = [f"{self._ref_emoji} {slug}"]
        if title:
            lines.append(f"  {title}")
        lines.append(f"  {n_blocks} blocks")

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
        lines.append(f"  get(id='{slug}/toc')     — structure")
        lines.append(f"  get(id='{slug}~0..10')   — first 10 chunks")
        lines.append(f"  get(id='{slug}/links')   — links graph")
        for hint in self._overview_hints(slug, ref):
            lines.append(f"  {hint}")
        return "\n".join(lines)

    def _overview_hints(self, slug: str, ref: dict) -> list[str]:
        """Return extra Next: hint lines for overview. Override in subclass."""
        return []

    def _read_meta(self, ref: dict) -> str:
        """Override in subclass for corpus-specific metadata display."""
        lines = []
        for key in ("slug", "title"):
            val = ref.get(key, "")
            if val:
                lines.append(f"  {key}: {val}")
        ref_id = ref.get("ref_id") or ref.get("id")
        if ref_id:
            lines.append(f"  ref_id: {ref_id}")
        return "\n".join(lines)

    def _list_header(self, count: int, grep: str = "") -> str:
        """Header line for list output. Override for custom emoji/noun."""
        if grep:
            return f"{self._ref_emoji} {count} {self._ref_noun}s matching '{grep}'"
        return f"{self._ref_emoji} {count} {self._ref_noun}s in library"

    def _list_entry(self, ref: dict) -> str:
        """Format a single ref for the list. Override for custom columns."""
        slug = ref.get("slug", "???")
        title = _truncate(ref.get("title", ""), 80)
        return f"  {slug}  {title}"

    # ── Resolve ──────────────────────────────────────────────────────

    def _resolve_ref(self, store, ident: str) -> dict[str, Any]:
        """Resolve identifier (slug, DOI, or ref_id) to a ref dict."""
        ref = store.get(ident)
        if ref is None:
            raise PrecisError(
                f"{self._ref_noun.title()} not found: {ident}\n"
                f"Try: get(grep='...') to filter, or search(query='...') to search."
            )
        return ref

    # ── List ─────────────────────────────────────────────────────────

    def _list_refs(self, store, grep: str = "") -> str:
        papers = store.list_papers()
        if not papers:
            return f"No {self._ref_noun}s in library."

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
                    f"No {self._ref_noun}s matching '{grep}'.\n"
                    "Try: get(grep='...') with different keywords, or /regex/i for regex."
                )

        lines = [self._list_header(len(papers), grep), ""]

        shown = papers[:self._max_list]
        for p in shown:
            lines.append(self._list_entry(p))

        lines.append("")
        if len(papers) > self._max_list:
            lines.append(
                f"  ... and {len(papers) - self._max_list} more "
                f"(showing first {self._max_list})"
            )
            lines.append("")
            lines.append("To find specific items:")
            lines.append("  search(query='...')  — semantic search")
            lines.append("  get(grep='...')      — filter by title/slug")
            lines.append("")
        lines.append(
            "Next: get(id='<slug>') for overview, "
            "get(id='<slug>/toc') for structure"
        )
        return "\n".join(lines)

    # ── TOC ──────────────────────────────────────────────────────────

    def _read_toc(self, store, ref: dict, selector: str | None = None) -> str:
        slug = ref.get("slug", "???")
        toc = store.get_toc(slug)
        if not toc:
            return f"No blocks found for {slug}"

        if selector:
            return self._read_toc_range(slug, toc, selector)

        if len(toc) <= self._TOC_OVERVIEW_THRESHOLD:
            return self._read_toc_flat(slug, toc)

        return self._read_toc_overview(slug, toc)

    def _read_toc_flat(self, slug: str, toc: list[dict]) -> str:
        return self._format_grouped_toc(
            slug, toc, f"{self._ref_emoji} {slug}  ({len(toc)} blocks)"
        )

    def _read_toc_overview(self, slug: str, toc: list[dict]) -> str:
        """Section-based overview TOC for large documents."""
        raw_groups: list[dict] = []
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

        # Merge tiny sections
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

        # Split oversized groups
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

        lines = [
            f"{self._ref_emoji} {slug}  "
            f"({len(toc)} blocks, {len(final_groups)} sections)",
            "",
        ]
        for g in final_groups:
            size = g["end"] - g["start"] + 1
            heading = g["heading"] or "(untitled)"
            snippet = ""
            heading_lower = heading.lower()
            for p in g.get("previews", []):
                if p.lower().strip() != heading_lower.strip():
                    snippet = _truncate(p, 80)
                    break
            line = f"  ~{g['start']}..{g['end']}  ({size})  {_truncate(heading, 60)}"
            if snippet:
                line += f"  — {snippet}"
            lines.append(line)

        lines.append("")
        lines.append(
            f"Next: get(id='{slug}~0..{min(60, len(toc)-1)}/toc') "
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
            raise PrecisError(f"Invalid range: {selector}\nUse ~N..M/toc")

        filtered = [e for e in toc
                    if start <= e.get("block_index", 0) <= end]
        if not filtered:
            return f"No blocks in ~{start}..{end} for {slug}"

        header = f"{self._ref_emoji} {slug}  ~{start}..{end}  ({len(filtered)} blocks)"
        result = self._format_grouped_toc(slug, filtered, header)

        last_idx = filtered[-1].get("block_index", end)
        max_idx = toc[-1].get("block_index", 0)
        if last_idx < max_idx:
            next_start = last_idx + 1
            result += (
                f"\nNext: get(id='{slug}~{next_start}..{min(next_start+60, max_idx)}/toc') "
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

            if section != current_section:
                current_section = section
                if section:
                    lines.append(f"  ~{idx}  §{section}")
                    if kind == "section_header":
                        continue

            mark = "✦" if has_summary else " "
            type_tag = f"  [{kind}]" if kind != "text" else ""
            snippet = f"  {_truncate(preview, 80)}" if preview else ""
            lines.append(f"    ~{idx}{mark}{type_tag}{snippet}")

        lines.append("")
        lines.append(f"Read: get(id='{slug}~N') for full chunk text")
        if has_summaries:
            lines.append(f"✦ = summary available: get(id='{slug}~N/summary')")
        return "\n".join(lines)

    # ── Chunks ───────────────────────────────────────────────────────

    def _read_chunks(self, store, ref: dict, selector: str) -> str:
        slug = ref.get("slug", "???")
        try:
            if ".." in selector:
                parts = selector.split("..")
                start = int(parts[0])
                end = int(parts[1]) if parts[1] else start + 10
            else:
                start = int(selector)
                end = start + 1
        except ValueError:
            raise PrecisError(
                f"Invalid chunk selector: {selector}\nUse ~N, ~N..M, or ~N.."
            )

        all_blocks = store.get_blocks(slug, block_type="text")
        blocks = [b for b in all_blocks
                  if start <= (b.get("block_index", 0)) < end]

        if not blocks:
            return f"No blocks in range ~{start}..{end} for {slug}"

        lines = []
        for block in blocks:
            idx = block.get("block_index", "?")
            kind = block.get("block_type", "text")
            text = block.get("text", "")
            page = block.get("page", "")
            lines.append(f">> {slug} ~{idx}  p{page}")
            lines.append(text)
            lines.append("")

        if end - start >= 10:
            lines.append(f"Next: get(id='{slug}~{end}..') for more")
        return "\n".join(lines)

    # ── Summary ──────────────────────────────────────────────────────

    def _read_summary(self, store, ref: dict, selector: str | None) -> str:
        slug = ref.get("slug", "???")
        if selector:
            try:
                idx = int(selector)
            except ValueError:
                raise PrecisError(f"Invalid chunk index: {selector}")
            blocks = store.get_blocks(slug, block_type="text")
            target = [b for b in blocks if b.get("block_index") == idx]
            if not target:
                raise PrecisError(f"Block ~{idx} not found in {slug}")
            summary = target[0].get("summary", "")
            return summary or f"No enrichment summary for block ~{idx}"
        blocks = store.get_blocks(slug, block_type="document_summary")
        if not blocks:
            blocks = store.get_blocks(slug, block_type="paper_summary")
        if blocks:
            return blocks[0].get("text", "")
        return f"No document-level summary for {slug}"

    # ── Links ────────────────────────────────────────────────────────

    def _read_links(self, store, ref: dict, selector: str | None) -> str:
        slug = ref.get("slug", "???")
        node_id = None
        if selector:
            try:
                block_idx = int(selector)
            except ValueError:
                raise PrecisError(f"Invalid block index: {selector}")
            blocks = store.get_blocks(slug, block_type="text")
            target = [b for b in blocks if b.get("block_index") == block_idx]
            if not target:
                raise PrecisError(f"Block ~{block_idx} not found in {slug}")
            node_id = target[0].get("node_id")

        links = store.get_links(slug, node_id=node_id)
        if not links:
            anchor = f"~{selector}" if selector else ""
            return (
                f"No links for {slug}{anchor}\n"
                f"Next:\n"
                f"  put(id='{slug}', link='other_slug:cites')  — create a link"
            )

        lines = [
            f"Links for {slug}"
            + (f"~{selector}" if selector else "")
            + f"  ({len(links)} total)"
        ]
        lines.append("")
        for link in links:
            direction = link.get("direction", "?")
            rel = link.get("display_relation", link.get("relation", "?"))
            if direction == "outbound":
                other = link.get("dst_slug", "?")
                arrow = f"  → [{rel}] → {other}"
                if link.get("dst_node_id"):
                    arrow += " (block)"
            else:
                other = link.get("src_slug", "?")
                arrow = f"  ← [{rel}] ← {other}"
                if link.get("src_node_id"):
                    arrow += " (block)"
            lines.append(arrow)

        lines.append("")
        lines.append("Next:")
        lines.append(f"  put(id='{slug}', link='other_slug:cites')  — add link")
        lines.append(f"  get(id='{slug}')  — overview")
        return "\n".join(lines)

    # ── Search ───────────────────────────────────────────────────────

    def _search_or_grep(self, store, query: str, top_k: int = 10) -> str:
        try:
            return self._search(store, query, top_k=top_k)
        except (ImportError, ModuleNotFoundError):
            log.info("Semantic search unavailable, falling back to keyword grep")
            return self._list_refs(store, grep=query)

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
            summary = hit.get("summary", "")
            snippet = summary or _truncate(text, 100)
            lines.append(f"  {slug}~{block_idx}  ({distance:.2f})  {snippet}")
        lines.append("")
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
            lines.append(f"  get(id='{s0}~{b0}')  — read this chunk")
            if len(seen) > 1:
                batch = ",".join(f"{s}~{b}" for s, b in seen)
                lines.append(f"  get(id='{batch}')  — batch read")
            lines.append(f"  get(id='{s0}/toc')  — structure")
        return "\n".join(lines)

    # ── Notes ────────────────────────────────────────────────────────

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
        ref = self._resolve_ref(store, path)
        slug = ref.get("slug", "???")
        ref_id = ref.get("ref_id") or ref.get("id")

        title = kwargs.get("title", "")
        tags = kwargs.get("tags", [])

        if selector:
            try:
                block_idx = int(selector)
            except ValueError:
                raise PrecisError(f"Invalid block index for note: {selector}")
            blocks = store.get_blocks(slug, block_type="text")
            target = [b for b in blocks if b.get("block_index") == block_idx]
            if not target:
                raise PrecisError(f"Block ~{block_idx} not found in {slug}")
            block_node_id = target[0].get("node_id")
            note_id = store.add_note(
                text,
                ref_id=ref_id,
                block_node_id=block_node_id,
                title=title or None,
                tags=tags or None,
                origin="bot",
            )
            return f"📝 Note #{note_id} on {slug}~{block_idx}\n{text}"
        else:
            note_id = store.add_note(
                text,
                ref_id=ref_id,
                title=title or None,
                tags=tags or None,
                origin="bot",
            )
            return f"📝 Note #{note_id} on {slug}\n{text}"
