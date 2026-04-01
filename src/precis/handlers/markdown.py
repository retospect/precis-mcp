"""Markdown handler — read/write for .md files.

Extends FileHandlerBase with a Markdown parser that recognises headings,
paragraphs, fenced code blocks, tables, and lists.  Write operations are
line-level edits on the plain-text source, identical in spirit to TexHandler.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

from precis.formatting import LIST_ITEM_RE, list_prefix, parse_list_prefix
from precis.handlers._file_base import FileHandlerBase
from precis.protocol import Node, PathCounter, PrecisError, make_slug, resolve_slug

# ── Regex helpers ────────────────────────────────────────────────────

_HEADING_RE = re.compile(r"^(#{1,4})\s+(.+)$")
_FENCE_RE = re.compile(r"^(`{3,}|~{3,})")
_TABLE_ROW_RE = re.compile(r"^\|.*\|$")
_TABLE_SEP_RE = re.compile(r"^\|[\s:*\-|]+\|$")
_THEMATIC_BREAK_RE = re.compile(r"^(?:---+|\*\*\*+|___+)\s*$")


class MarkdownHandler(FileHandlerBase):
    """Handler for .md / .markdown files."""

    extensions = {".md", ".markdown"}

    # ── Parser implementation ───────────────────────────────────────

    def parse(self, path: Path) -> list[Node]:
        """Parse a Markdown file into a list of Nodes."""
        content = path.read_text(encoding="utf-8")
        lines = content.split("\n")
        counter = PathCounter()
        slug_counts: dict[str, int] = {}
        nodes: list[Node] = []
        filename = path.name
        i = 0

        while i < len(lines):
            line = lines[i]
            stripped = line.strip()

            # Skip blank lines
            if not stripped:
                i += 1
                continue

            # Skip thematic breaks (---, ***, ___)
            if _THEMATIC_BREAK_RE.match(stripped):
                i += 1
                continue

            # ── Heading ──────────────────────────────────────────
            h_match = _HEADING_RE.match(stripped)
            if h_match:
                level = len(h_match.group(1))
                title = h_match.group(2).strip()
                node_path = counter.next_heading(level)
                base_slug = make_slug(title)
                slug = resolve_slug(base_slug, slug_counts)
                nodes.append(
                    Node(
                        slug=slug,
                        path=node_path,
                        node_type="h",
                        text=title,
                        precis=title,
                        style=f"h{level}",
                        source_file=filename,
                        source_line_start=i + 1,
                        source_line_end=i + 1,
                    )
                )
                i += 1
                continue

            # ── Fenced code block ────────────────────────────────
            fence_match = _FENCE_RE.match(stripped)
            if fence_match:
                fence_char = fence_match.group(1)
                fence_len = len(fence_char)
                fence_ch = fence_char[0]
                start_line = i + 1
                code_lines = [line]
                i += 1
                while i < len(lines):
                    code_lines.append(lines[i])
                    cl = lines[i].strip()
                    if cl.startswith(fence_ch * fence_len) and len(cl) >= fence_len and cl == fence_ch * len(cl):
                        i += 1
                        break
                    i += 1
                end_line = start_line + len(code_lines) - 1
                text = "\n".join(code_lines)
                # Extract language hint from opening fence
                lang_hint = stripped[fence_len:].strip().split()[0] if stripped[fence_len:].strip() else ""
                precis_text = f"[code{': ' + lang_hint if lang_hint else ''}]"

                node_path = counter.next_child("p")
                base_slug = make_slug(text)
                slug = resolve_slug(base_slug, slug_counts)
                nodes.append(
                    Node(
                        slug=slug,
                        path=node_path,
                        node_type="p",
                        text=text,
                        precis=precis_text,
                        style="code",
                        source_file=filename,
                        source_line_start=start_line,
                        source_line_end=end_line,
                    )
                )
                continue

            # ── Table ────────────────────────────────────────────
            if _TABLE_ROW_RE.match(stripped):
                start_line = i + 1
                table_lines = []
                while i < len(lines) and _TABLE_ROW_RE.match(lines[i].strip()):
                    table_lines.append(lines[i])
                    i += 1
                end_line = start_line + len(table_lines) - 1
                text = "\n".join(table_lines)
                synopsis = _table_synopsis(table_lines)

                node_path = counter.next_child("t")
                base_slug = make_slug(text)
                slug = resolve_slug(base_slug, slug_counts)
                nodes.append(
                    Node(
                        slug=slug,
                        path=node_path,
                        node_type="t",
                        text=text,
                        precis=synopsis,
                        style="table",
                        source_file=filename,
                        source_line_start=start_line,
                        source_line_end=end_line,
                    )
                )
                continue

            # ── List block ───────────────────────────────────────
            li = parse_list_prefix(stripped)
            if li:
                start_line = i + 1
                list_lines = []
                while i < len(lines):
                    ln = lines[i].strip()
                    if not ln:
                        break
                    # Still a list item or continuation indent
                    if parse_list_prefix(ln) or (list_lines and ln and not _HEADING_RE.match(ln)):
                        list_lines.append(lines[i])
                        i += 1
                    else:
                        break
                end_line = start_line + len(list_lines) - 1
                text = "\n".join(list_lines)

                node_path = counter.next_child("p")
                base_slug = make_slug(text)
                slug = resolve_slug(base_slug, slug_counts)
                nodes.append(
                    Node(
                        slug=slug,
                        path=node_path,
                        node_type="p",
                        text=text,
                        style="list",
                        source_file=filename,
                        source_line_start=start_line,
                        source_line_end=end_line,
                    )
                )
                continue

            # ── Paragraph ────────────────────────────────────────
            start_line = i + 1
            para_lines = []
            while i < len(lines):
                ln = lines[i].strip()
                if not ln:
                    break
                if _HEADING_RE.match(ln):
                    break
                if _FENCE_RE.match(ln):
                    break
                if _TABLE_ROW_RE.match(ln):
                    break
                if _THEMATIC_BREAK_RE.match(ln):
                    break
                para_lines.append(lines[i])
                i += 1

            if para_lines:
                end_line = start_line + len(para_lines) - 1
                text = "\n".join(para_lines)

                node_path = counter.next_child("p")
                base_slug = make_slug(text)
                slug = resolve_slug(base_slug, slug_counts)
                nodes.append(
                    Node(
                        slug=slug,
                        path=node_path,
                        node_type="p",
                        text=text,
                        source_file=filename,
                        source_line_start=start_line,
                        source_line_end=end_line,
                    )
                )
            else:
                i += 1

        return nodes

    def source_files(self, path: Path) -> list[Path]:
        return [path]

    # ── Write operations ─────────────────────────────────────────

    def write_node(self, path: Path, node: Node, new_text: str) -> None:
        lines = _read_lines(path)
        start = node.source_line_start - 1
        end = node.source_line_end
        new_lines = new_text.splitlines(keepends=True)
        if new_lines and not new_lines[-1].endswith("\n"):
            new_lines[-1] += "\n"
        lines[start:end] = new_lines
        _atomic_write(path, "".join(lines))

    def insert_after(
        self, path: Path, anchor: Node, new_text: str, heading_level: int = 0
    ) -> None:
        lines = _read_lines(path)
        if heading_level:
            new_text = "#" * heading_level + " " + new_text
        insert_at = anchor.source_line_end
        lines.insert(insert_at, "\n" + new_text + "\n")
        _atomic_write(path, "".join(lines))

    def insert_before(
        self, path: Path, anchor: Node, new_text: str, heading_level: int = 0
    ) -> None:
        lines = _read_lines(path)
        if heading_level:
            new_text = "#" * heading_level + " " + new_text
        insert_at = anchor.source_line_start - 1
        lines.insert(insert_at, new_text + "\n\n")
        _atomic_write(path, "".join(lines))

    def delete_node(self, path: Path, node: Node) -> None:
        lines = _read_lines(path)
        start = node.source_line_start - 1
        end = node.source_line_end
        del lines[start:end]
        _atomic_write(path, "".join(lines))

    def append_node(self, path: Path, new_text: str, heading_level: int = 0) -> None:
        content = path.read_text(encoding="utf-8")
        if heading_level:
            new_text = "#" * heading_level + " " + new_text
        content = content.rstrip() + "\n\n" + new_text + "\n"
        _atomic_write(path, content)

    def move_nodes(self, path: Path, nodes: list[Node], after: Node) -> None:
        # Collect texts from source positions
        texts = []
        for n in nodes:
            lines = _read_lines(path)
            start = n.source_line_start - 1
            end = n.source_line_end
            texts.append("".join(lines[start:end]))

        # Delete nodes (reverse order to preserve line numbers)
        for n in reversed(nodes):
            self.delete_node(path, n)

        # Re-parse to find the anchor in its new position
        fresh_nodes = self.parse(path)
        fresh_after = None
        for fn in fresh_nodes:
            if fn.slug == after.slug:
                fresh_after = fn
                break
        if fresh_after is None:
            raise PrecisError(f"Anchor not found after deletion: {after.slug}")

        # Insert collected texts after anchor
        lines = _read_lines(path)
        insert_at = fresh_after.source_line_end
        for text in reversed(texts):
            lines.insert(insert_at, "\n" + text)
        _atomic_write(path, "".join(lines))


# ── Module-level helpers ─────────────────────────────────────────────


def _read_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines(keepends=True)


def _atomic_write(path: Path, content: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".md.tmp")
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


def _table_synopsis(table_lines: list[str]) -> str:
    """Generate a short synopsis from a markdown table."""
    # Use header row (first non-separator row)
    for line in table_lines:
        stripped = line.strip()
        if _TABLE_SEP_RE.match(stripped):
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        nrows = sum(1 for ln in table_lines if not _TABLE_SEP_RE.match(ln.strip())) - 1
        return "|".join(cells) + f" ({nrows}r)"
    return "[table]"
