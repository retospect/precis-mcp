"""Shared base for file-based handlers (word, tex).

Provides the common read/put dispatch logic: toc formatting, node resolution,
grep filtering, depth filtering, verbosity rules, and put mode dispatch.
Subclasses provide the parser and format-specific operations.
"""

from __future__ import annotations

import logging
import re
from abc import abstractmethod
from pathlib import Path

from precis_summary.rake import telegram_precis

from precis.citations import BIB_DEF_RE, CITE_RE, MALFORMED_CHUNK_RE, MALFORMED_NO_AT_RE
from precis.config import PrecisConfig
from precis.formatting import group_paragraphs
from precis.grep import parse_grep
from precis.output import (
    ANNOTATION,
    DERIVED,
    format_hints,
    format_node_full,
    format_node_precis,
)
from precis.protocol import Handler, Node, PrecisError

log = logging.getLogger(__name__)

_LARGE_DOC_THRESHOLD = 100
_SECTION_NUM_RE = re.compile(r"^[\d]+(?:\.[\d]+)*\.?\s+")


class FileHandlerBase(Handler):
    """Abstract base for file:-scheme handlers.

    Subclasses must implement the parser interface methods.
    """

    scheme = "file"
    writable = True
    views = {"toc", "meta"}

    def __init__(self):
        self.config = PrecisConfig.load()

    # ── Parser interface (subclass must implement) ──────────────────

    @abstractmethod
    def parse(self, path: Path) -> list[Node]:
        """Parse document into nodes."""
        ...

    @abstractmethod
    def source_files(self, path: Path) -> list[Path]:
        """Return all source files involved."""
        ...

    @abstractmethod
    def write_node(self, path: Path, node: Node, new_text: str) -> None:
        """Replace a node's text content on disk."""
        ...

    @abstractmethod
    def insert_after(
        self, path: Path, anchor: Node, new_text: str, heading_level: int = 0
    ) -> None: ...

    @abstractmethod
    def insert_before(
        self, path: Path, anchor: Node, new_text: str, heading_level: int = 0
    ) -> None: ...

    @abstractmethod
    def delete_node(self, path: Path, node: Node) -> None: ...

    @abstractmethod
    def append_node(
        self, path: Path, new_text: str, heading_level: int = 0
    ) -> None: ...

    @abstractmethod
    def move_nodes(self, path: Path, nodes: list[Node], after: Node) -> None: ...

    # ── Optional overrides ──────────────────────────────────────────

    def write_tracked(
        self, path: Path, node: Node, new_text: str, author: str = "precis"
    ) -> None:
        """Replace with track-changes markup. Default: plain write."""
        self.write_node(path, node, new_text)

    def write_comment(
        self, path: Path, node: Node, text: str, author: str = "precis"
    ) -> int:
        """Add a margin comment. Default: not supported."""
        raise PrecisError("Comments not supported for this file type")

    # ── Shared helpers ──────────────────────────────────────────────

    def _load_nodes(self, file_path: str) -> list[Node]:
        """Parse fresh from disk and generate RAKE precis."""
        path = Path(file_path)
        nodes = self.parse(path)
        # Assign sequential indices
        for i, n in enumerate(nodes):
            n.index = i
        self._apply_precis(nodes)
        return nodes

    def _apply_precis(self, nodes: list[Node]) -> None:
        """Generate RAKE precis for content nodes that don't already have one."""
        for node in nodes:
            if node.precis:
                continue
            if node.node_type in ("p", "t", "f", "e"):
                node.precis = telegram_precis(node.text)

    def _build_index(self, nodes: list[Node]) -> dict[str, Node]:
        """Build slug→node, path→node, label→node index."""
        index: dict[str, Node] = {}
        for n in nodes:
            index[n.slug] = n
            index[str(n.path)] = n
            if n.label:
                index[n.label] = n
        return index

    def _resolve_node(self, selector: str, index: dict[str, Node]) -> Node:
        """Resolve a selector string to a single Node."""
        node = index.get(selector)
        if node is None:
            if selector.startswith("#") or len(selector) > 10 or " " in selector:
                slugs = [k for k in index if not k.startswith("S") and "." not in k]
                slug_list = ", ".join(slugs[:8])
                raise PrecisError(
                    f"'{selector}' is not a valid SLUG.\n"
                    "Use a 5-char slug from toc output, not heading text.\n"
                    f"Available slugs: {slug_list}"
                )
            raise PrecisError(
                f"slug '{selector}' not found\n"
                "The document may have changed. Re-read to refresh slugs."
            )
        return node

    # ── Handler.read implementation ─────────────────────────────────

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
    ) -> str:
        file_path = self._resolve_path(path)

        # Bare URI with no selector: toc
        if not selector and not query and view in (None, "toc"):
            return self._read_toc(file_path, depth=depth, summarize=summarize)

        # View: meta
        if view == "meta":
            return self._read_meta(file_path)

        # Query: grep/search
        if query:
            scope = selector  # selector acts as scope filter for query
            return self._read_query(file_path, query, scope=scope, summarize=summarize)

        # Selector: specific node(s)
        if selector:
            return self._read_selector(
                file_path, selector, depth=depth, summarize=summarize
            )

        # Fallback: toc
        return self._read_toc(file_path, depth=depth, summarize=summarize)

    def _read_toc(
        self, file_path: str, depth: int = 0, summarize: bool = False, scope: str = ""
    ) -> str:
        path = Path(file_path)
        nodes = self._load_nodes(file_path)
        total_nodes = len(nodes)

        # Filter by scope
        if scope:
            nodes = [n for n in nodes if str(n.path).startswith(scope)]

        # Auto-adaptive: large docs default to headings-only
        auto_truncated = False
        effective_depth = depth
        if depth == 0 and len(nodes) > _LARGE_DOC_THRESHOLD and not scope:
            effective_depth = 4
            auto_truncated = True

        # Apply depth filter
        if effective_depth > 0:
            nodes = [
                n
                for n in nodes
                if n.node_type == "h" and n.heading_level() <= effective_depth
            ]

        # Format header
        header = f"📄 {path.name}"
        if scope:
            header += f"  scope: {scope}"
        if effective_depth > 0:
            header += f"  depth: {effective_depth}"
        header += f"  ({len(nodes)} nodes"
        if len(nodes) != total_nodes:
            header += f" / {total_nodes} total"
        header += ")"

        if not nodes and total_nodes == 0:
            return (
                f"{header}\n\n"
                "The document is empty.\n"
                f"Start writing with put(id='{path.name}', text='# | Introduction', mode='append')"
            )

        lines = [header, ""]
        for node in nodes:
            lines.append(
                format_node_precis(
                    node, show_slug=True, show_source=bool(node.source_file)
                )
            )

        if auto_truncated:
            lines.append("")
            lines.append(
                f"⚠ Large document ({total_nodes} nodes) — showing headings only."
            )
            lines.append(
                f"Drill in: get(id='{path.name}~S3.2') for section detail, "
                f"get(id='{path.name}', depth=2) for outline."
            )

        # Hints
        hints = []
        if nodes:
            first_slug = nodes[0].slug
            hints.append(f"get(id='{path.name}~{first_slug}')")
        if total_nodes > 0:
            hints.append(f"get(id='{path.name}', grep='...')")
        hints.append(f"put(id='{path.name}', text='...', mode='append')")
        lines.append(format_hints(hints))

        return "\n".join(lines)

    def _read_meta(self, file_path: str) -> str:
        path = Path(file_path)
        nodes = self._load_nodes(file_path)
        source_files = self.source_files(path)

        lines = [f"📄 {path.name}"]
        lines.append(f"  nodes: {len(nodes)}")
        lines.append(f"  headings: {sum(1 for n in nodes if n.node_type == 'h')}")
        lines.append(f"  paragraphs: {sum(1 for n in nodes if n.node_type == 'p')}")
        lines.append(f"  tables: {sum(1 for n in nodes if n.node_type == 't')}")
        lines.append(f"  figures: {sum(1 for n in nodes if n.node_type == 'f')}")
        lines.append(f"  equations: {sum(1 for n in nodes if n.node_type == 'e')}")
        lines.append(f"  bibliography: {sum(1 for n in nodes if n.node_type == 'b')}")
        lines.append(f"  files: {len(source_files)}")
        for sf in source_files:
            try:
                lc = len(sf.read_text(encoding="utf-8").splitlines())
            except Exception:
                lc = 0
            lines.append(f"    {sf.name}  {lc} lines")

        # Word count
        total_words = sum(len(n.text.split()) for n in nodes)
        lines.append(f"  words: ~{total_words}")

        return "\n".join(lines)

    def _read_query(
        self,
        file_path: str,
        query: str,
        scope: str | None = None,
        summarize: bool = False,
    ) -> str:
        path = Path(file_path)
        nodes = self._load_nodes(file_path)

        if scope:
            nodes = [n for n in nodes if str(n.path).startswith(scope)]

        pattern = parse_grep(query)
        hits = [
            n for n in nodes if pattern.matches(n.text) or pattern.matches(n.precis)
        ]

        header = f"📄 {path.name}  query: {query}  ({len(hits)} hits)"
        lines = [header, ""]
        for h in hits:
            lines.append(
                format_node_precis(h, show_slug=True, show_source=bool(h.source_file))
            )

        if not hits:
            lines.append("No matches.")
            lines.append(
                f"Try: get(id='{path.name}', grep='...') with different keywords"
            )

        return "\n".join(lines)

    def _read_selector(
        self, file_path: str, selector: str, depth: int = 0, summarize: bool = False
    ) -> str:
        path = Path(file_path)
        nodes = self._load_nodes(file_path)
        index = self._build_index(nodes)

        # Path-based scope: #S1.2 → show scoped toc
        if selector.startswith("S") and selector not in index:
            return self._read_toc(
                file_path, depth=depth, summarize=summarize, scope=selector
            )

        # Comma-separated: multi-node get
        parts = [p.strip() for p in selector.split(",") if p.strip()]
        output_lines: list[str] = []

        for part in parts:
            node = self._resolve_node(part, index)

            if node.path.is_heading():
                # Return heading + all children
                section_nodes = [node]
                for n in nodes:
                    if n is not node and n.path.is_child_of(node.path):
                        section_nodes.append(n)

                for n in section_nodes:
                    if summarize or len(section_nodes) > 1:
                        output_lines.append(
                            format_node_precis(
                                n, show_slug=True, show_source=bool(n.source_file)
                            )
                        )
                    else:
                        output_lines.append(
                            format_node_full(
                                n, show_slug=True, show_source=bool(n.source_file)
                            )
                        )
            else:
                # Single content node: full text
                output_lines.append(
                    format_node_full(
                        node, show_slug=True, show_source=bool(node.source_file)
                    )
                )

        return "\n".join(output_lines)

    # ── Handler.put implementation ──────────────────────────────────

    def put(
        self,
        path: str,
        selector: str | None,
        text: str,
        mode: str,
        **kwargs,
    ) -> str:
        file_path = self._resolve_path(path)
        tracked = kwargs.get("tracked", True)
        text = text.strip() if text else ""

        valid_modes = {"replace", "after", "before", "delete", "append", "move", "note"}
        if mode not in valid_modes:
            raise PrecisError(
                f"invalid mode: {mode}\nValid modes: {', '.join(sorted(valid_modes))}"
            )

        if mode == "note":
            return self._put_note(file_path, selector, text)

        if mode == "move":
            return self._put_move(file_path, selector, text)

        if mode == "append":
            return self._put_append(file_path, text, tracked)

        if not selector:
            raise PrecisError(
                f"selector required for mode={mode}. "
                "To write new content, use mode='append'. "
                "To edit existing content, add ~SLUG to the id."
            )

        if mode == "delete":
            return self._put_delete(file_path, selector)

        if mode == "replace":
            return self._put_replace(file_path, selector, text, tracked)

        if mode in ("after", "before"):
            return self._put_insert(file_path, selector, text, mode, tracked)

        raise PrecisError(f"unhandled mode: {mode}")

    def _put_append(self, file_path: str, text: str, tracked: bool) -> str:
        if not text:
            raise PrecisError("text required for mode=append")

        fpath = Path(file_path)
        nodes_before = self._load_nodes(file_path)

        chunks = group_paragraphs(text)
        for chunk in chunks:
            heading_level = _heading_level_from_text(chunk)
            clean = _clean_heading(chunk) if heading_level else chunk
            self.append_node(fpath, clean, heading_level)

        nodes_after = self._load_nodes(file_path)
        old_slugs = {n.slug for n in nodes_before}
        new_nodes = [n for n in nodes_after if n.slug not in old_slugs]

        lines = []
        for nn in new_nodes:
            precis = nn.precis or nn.text[:60]
            lines.append(f"+ {nn.slug}  {nn.path}  {precis}")

        result = "\n".join(lines) if lines else "+ appended"
        fname = Path(file_path).name
        if new_nodes:
            last = new_nodes[-1]
            result += (
                f"\n\nNext:\n  put(id='{fname}~{last.slug}', text='...', mode='after')"
            )
        if len(new_nodes) > 3:
            result += f"\n  get(id='{fname}', depth=2)  — review outline"
        result += self._citation_hints(file_path)
        return result

    def _put_delete(self, file_path: str, selector: str) -> str:
        fpath = Path(file_path)
        nodes = self._load_nodes(file_path)
        index = self._build_index(nodes)
        node = self._resolve_node(selector, index)
        self.delete_node(fpath, node)
        return f"- {node.slug}  {node.path}  deleted"

    def _put_replace(
        self, file_path: str, selector: str, text: str, tracked: bool
    ) -> str:
        if not text:
            raise PrecisError("text required for replace mode")

        fpath = Path(file_path)
        nodes = self._load_nodes(file_path)
        index = self._build_index(nodes)
        node = self._resolve_node(selector, index)

        chunks = group_paragraphs(text)

        # First chunk replaces the target node
        first = chunks[0]
        heading_level = _heading_level_from_text(first)
        clean = _clean_heading(first) if heading_level else first

        if tracked:
            self.write_tracked(fpath, node, clean, self.config.author)
        else:
            self.write_node(fpath, node, clean)

        # Insert remaining chunks after the replaced node
        self._insert_chunks_after(file_path, node.index, chunks[1:])

        new_nodes = self._load_nodes(file_path)
        new_node = None
        # Match by index (position), not path — path changes when headings change
        for nn in new_nodes:
            if nn.index == node.index:
                new_node = nn
                break

        if new_node:
            tracked_label = " tracked" if tracked else ""
            precis = new_node.precis or clean
            lines = [
                f"{node.slug} → {new_node.slug}  {node.path}{tracked_label}  replace",
                f"{DERIVED}  {precis}",
            ]
            for i in range(1, len(chunks)):
                idx = node.index + i
                if idx < len(new_nodes):
                    en = new_nodes[idx]
                    lines.append(f"+ {en.slug}  {en.path}")
            return "\n".join(lines) + self._citation_hints(file_path)
        return f"{node.slug} → ???  {node.path}  replace"

    def _put_insert(
        self, file_path: str, selector: str, text: str, mode: str, tracked: bool
    ) -> str:
        if not text:
            raise PrecisError(f"text required for {mode} mode")

        fpath = Path(file_path)
        nodes = self._load_nodes(file_path)
        index = self._build_index(nodes)
        node = self._resolve_node(selector, index)

        chunks = group_paragraphs(text)

        # First chunk: insert before or after anchor
        first = chunks[0]
        hl = _heading_level_from_text(first)
        clean = _clean_heading(first) if hl else first

        if mode == "after":
            self.insert_after(fpath, node, clean, hl)
            # New node lands right after anchor
            first_new_idx = node.index + 1
        else:
            self.insert_before(fpath, node, clean, hl)
            # New node takes the anchor's old position
            first_new_idx = node.index

        # Insert remaining chunks after the first inserted one
        self._insert_chunks_after(file_path, first_new_idx, chunks[1:])

        new_nodes = self._load_nodes(file_path)
        old_slugs = {n.slug for n in nodes}
        new_added = [nn for nn in new_nodes if nn.slug not in old_slugs]

        if new_added:
            lines = []
            for nn in new_added:
                precis = nn.precis or (nn.text[:60] if nn.text else "")
                lines.append(f"+ {nn.slug}  {nn.path}  {mode} {node.slug}")
            lines.append(f"{DERIVED}  {new_added[0].precis or clean}")
            return "\n".join(lines) + self._citation_hints(file_path)
        return f"+ ???  {mode} {node.slug}"

    def _insert_chunks_after(
        self, file_path: str, anchor_idx: int, chunks: list[str]
    ) -> None:
        """Insert chunks sequentially, each after the previous one."""
        fpath = Path(file_path)
        for chunk in chunks:
            current_nodes = self._load_nodes(file_path)
            if anchor_idx >= len(current_nodes):
                break
            anchor = current_nodes[anchor_idx]
            hl = _heading_level_from_text(chunk)
            c = _clean_heading(chunk) if hl else chunk
            self.insert_after(fpath, anchor, c, hl)
            anchor_idx += 1

    def _put_move(self, file_path: str, selector: str | None, target_slug: str) -> str:
        if not selector:
            raise PrecisError("selector required for move mode (node to move)")
        if not target_slug:
            raise PrecisError("text required for move mode (target slug to move after)")

        fpath = Path(file_path)
        nodes = self._load_nodes(file_path)
        index = self._build_index(nodes)

        parts = [p.strip() for p in selector.split(",") if p.strip()]
        move_list = [self._resolve_node(p, index) for p in parts]
        after_node = self._resolve_node(target_slug, index)

        self.move_nodes(fpath, move_list, after_node)

        new_nodes = self._load_nodes(file_path)
        new_index = self._build_index(new_nodes)
        lines = []
        for mn in move_list:
            nn = new_index.get(mn.slug)
            if nn:
                lines.append(f"moved {mn.slug} {mn.path} → {nn.path}")
            else:
                lines.append(f"moved {mn.slug} {mn.path} → ???")
        return "\n".join(lines)

    def _put_note(self, file_path: str, selector: str | None, text: str) -> str:
        """Add a margin comment / annotation."""
        if not text:
            raise PrecisError("text required for note mode")
        if not selector:
            raise PrecisError("selector required for note mode")

        fpath = Path(file_path)
        nodes = self._load_nodes(file_path)
        index = self._build_index(nodes)
        node = self._resolve_node(selector, index)

        comment_id = self.write_comment(fpath, node, text, self.config.author)
        return (
            f"💬 {node.slug}  {node.path}  comment #{comment_id}\n{ANNOTATION} {text}"
        )

    def _citation_hints(self, file_path: str) -> str:
        """Scan for undefined/malformed citations and return hint text."""
        nodes = self._load_nodes(file_path)
        inline_keys: set[str] = set()
        defined_keys: set[str] = set()
        malformed: list[tuple[str, str, str]] = []  # (bad, slug, fix)
        for node in nodes:
            text = node.text or ""
            for m in CITE_RE.finditer(text):
                inline_keys.add(m.group(1))
            if node.node_type == "b" and node.label:
                defined_keys.add(node.label)
            for m in BIB_DEF_RE.finditer(text):
                defined_keys.add(m.group(1))
            # Detect malformed citations
            for m in MALFORMED_CHUNK_RE.finditer(text):
                slug = m.group(1)
                malformed.append((m.group(0), slug, f"[@{slug}]"))
            for m in MALFORMED_NO_AT_RE.finditer(text):
                slug = m.group(1)
                malformed.append((m.group(0), slug, f"[@{slug}]"))

        parts: list[str] = []
        fname = Path(file_path).name

        if malformed:
            seen: set[str] = set()
            unique = []
            for bad, slug, fix in malformed:
                if bad not in seen:
                    seen.add(bad)
                    unique.append((bad, slug, fix))
            parts.append(
                f"\n\n⚠ {len(unique)} malformed citation(s) — use [@slug], never [slug] or [slug~N]:"
            )
            for bad, slug, fix in unique:
                parts.append(f"  {bad} → {fix}")

        undefined = sorted(inline_keys - defined_keys)
        if undefined:
            parts.append(f"\n\n⚠ {len(undefined)} undefined citation(s):")
            for key in undefined:
                parts.append(
                    f"  put(id='{fname}', text='[@{key}]: <full reference>', mode='append')"
                )
            first = undefined[0]
            parts.append(f"Tip: get(id='{first}/cite') to look up citation text")

        return "\n".join(parts) if parts else ""

    def _resolve_path(self, path: str) -> str:
        """Resolve and validate the file path."""
        # For now: treat as-is (relative or absolute)
        p = Path(path)
        if not p.exists():
            # Try creating DOCX/TeX if missing
            if path.endswith(".docx"):
                p.parent.mkdir(parents=True, exist_ok=True)
                from docx import Document

                doc = Document()
                doc.save(str(p))
            elif path.endswith(".tex"):
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(
                    "\\documentclass{article}\n\\begin{document}\n\n\\end{document}\n",
                    encoding="utf-8",
                )
            elif (
                path.endswith(".md")
                or path.endswith(".markdown")
                or path.endswith(".txt")
                or path.endswith(".text")
            ):
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text("", encoding="utf-8")
            else:
                raise PrecisError(f"File not found: {path}")
        return str(p)


# ── Text helpers ────────────────────────────────────────────────────


def _heading_level_from_text(text: str) -> int:
    """Detect # prefix for heading level."""
    stripped = text.lstrip()
    level = 0
    for ch in stripped:
        if ch == "#":
            level += 1
        else:
            break
    return min(level, 4)


def _clean_heading(text: str) -> str:
    """Strip # prefix and section numbering from heading text."""
    stripped = text.lstrip("#").strip()
    # Strip section numbering: "3.3 | Foo" → "Foo", "3.3 Foo" → "Foo"
    if "|" in stripped:
        return stripped.split("|", 1)[1].strip()
    return _SECTION_NUM_RE.sub("", stripped)
