"""Plain-text handler — read/write for .txt files.

Extends FileHandlerBase with a paragraph-based parser.  Paragraphs are
contiguous runs of non-blank lines separated by blank lines.  No heading
hierarchy — all nodes are flat paragraphs under S0.

Write operations are line-level edits on the source, same as MarkdownHandler.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from precis.handlers._file_base import FileHandlerBase
from precis.protocol import Node, PathCounter, PrecisError, make_slug, resolve_slug


class PlainTextHandler(FileHandlerBase):
    """Handler for .txt / .text files."""

    extensions = {".txt", ".text"}

    # ── Parser implementation ───────────────────────────────────────

    def parse(self, path: Path) -> list[Node]:
        """Parse a plain-text file into paragraph Nodes."""
        content = path.read_text(encoding="utf-8")
        lines = content.split("\n")
        counter = PathCounter()
        slug_counts: dict[str, int] = {}
        nodes: list[Node] = []
        filename = path.name
        i = 0

        while i < len(lines):
            # Skip blank lines
            if not lines[i].strip():
                i += 1
                continue

            # Collect contiguous non-blank lines → one paragraph
            start_line = i + 1  # 1-indexed
            para_lines = []
            while i < len(lines) and lines[i].strip():
                para_lines.append(lines[i])
                i += 1

            text = "\n".join(para_lines)
            end_line = start_line + len(para_lines) - 1

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
        insert_at = anchor.source_line_end
        lines.insert(insert_at, "\n" + new_text + "\n")
        _atomic_write(path, "".join(lines))

    def insert_before(
        self, path: Path, anchor: Node, new_text: str, heading_level: int = 0
    ) -> None:
        lines = _read_lines(path)
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
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".txt.tmp")
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
