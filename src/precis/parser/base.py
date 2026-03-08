"""Abstract base parser for document formats."""

from __future__ import annotations

import abc
from pathlib import Path

from precis.nodes import Node


class BaseParser(abc.ABC):
    """Base class for document parsers."""

    @abc.abstractmethod
    def parse(self, path: Path) -> list[Node]:
        """Parse a document and return a list of Nodes with paths assigned."""
        ...

    @abc.abstractmethod
    def source_files(self, path: Path) -> list[Path]:
        """Return all source files involved (for hash computation).

        For DOCX: just the single file.
        For LaTeX: root + all \\input/\\include'd files.
        """
        ...

    @abc.abstractmethod
    def write_node(self, path: Path, node: Node, new_text: str) -> None:
        """Replace a node's text content on disk."""
        ...

    @abc.abstractmethod
    def insert_after(
        self, path: Path, anchor: Node, new_text: str, heading_level: int = 0
    ) -> None:
        """Insert a new paragraph after the anchor node."""
        ...

    @abc.abstractmethod
    def insert_before(
        self, path: Path, anchor: Node, new_text: str, heading_level: int = 0
    ) -> None:
        """Insert a new paragraph before the anchor node."""
        ...

    @abc.abstractmethod
    def delete_node(self, path: Path, node: Node) -> None:
        """Delete a node from the document."""
        ...

    @abc.abstractmethod
    def append_node(self, path: Path, new_text: str, heading_level: int = 0) -> None:
        """Append a node to the end of the document."""
        ...

    @abc.abstractmethod
    def move_nodes(self, path: Path, nodes: list[Node], after: Node) -> None:
        """Move nodes to after the target node."""
        ...
