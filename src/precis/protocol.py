"""Handler protocol, Node model, and shared types.

This module defines the abstract base class that every document handler must
implement, plus the Node/Path data structures shared across all handlers.
"""

from __future__ import annotations

import abc
import hashlib
import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class PrecisError(Exception):
    """Error that formats as !! ERROR for the LLM."""

    def format(self) -> str:
        return f"!! ERROR {self}"


# ---------------------------------------------------------------------------
# Slugs
# ---------------------------------------------------------------------------

SLUG_CHARS = "0123456789ABCDEFGHJKLMNPQRSTUVWXYZ"
SLUG_LEN = 5


def make_slug(text: str) -> str:
    """Generate a 5-char base34 content slug from text."""
    h = int(hashlib.sha256(text.strip().encode()).hexdigest()[:8], 16)
    out = []
    for _ in range(SLUG_LEN):
        h, r = divmod(h, len(SLUG_CHARS))
        out.append(SLUG_CHARS[r])
    return "".join(out)


def resolve_slug(slug: str, slug_counts: dict[str, int]) -> str:
    """Resolve a slug with collision suffix if needed."""
    count = slug_counts.get(slug, 0) + 1
    slug_counts[slug] = count
    if count == 1:
        return slug
    return f"{slug}.{count}"


# ---------------------------------------------------------------------------
# Path — hierarchical position within a document
# ---------------------------------------------------------------------------

PATH_RE = re.compile(
    r"^S(\d+)(?:\.(\d+)(?:\.(\d+)(?:\.(\d+))?)?)?(?:([ptfeb¶])(\d+))?$"
)

_TYPE_DISPLAY = {"p": "¶"}
_TYPE_INTERNAL = {"¶": "p"}


@dataclass
class Path:
    """Positional heading-path ID: S1.2¶3."""

    h1: int = 0
    h2: int = 0
    h3: int = 0
    h4: int = 0
    node_type: str = ""  # p, t, f, e, b, or "" for headings
    index: int = 0  # 1-indexed within parent section

    def __str__(self) -> str:
        parts = [str(self.h1)]
        if self.h2 or self.h3 or self.h4:
            parts.append(str(self.h2))
        if self.h3 or self.h4:
            parts.append(str(self.h3))
        if self.h4:
            parts.append(str(self.h4))
        base = "S" + ".".join(parts)
        if self.node_type:
            display = _TYPE_DISPLAY.get(self.node_type, self.node_type)
            return f"{base}{display}{self.index}"
        return base

    @classmethod
    def parse(cls, s: str) -> Path:
        """Parse a path string like S1.2¶3."""
        m = PATH_RE.match(s)
        if not m:
            raise ValueError(f"Invalid path: {s!r}")
        h1 = int(m[1])
        h2 = int(m[2]) if m[2] is not None else 0
        h3 = int(m[3]) if m[3] is not None else 0
        h4 = int(m[4]) if m[4] is not None else 0
        node_type = _TYPE_INTERNAL.get(m[5], m[5]) if m[5] else ""
        index = int(m[6]) if m[6] else 0
        return cls(h1=h1, h2=h2, h3=h3, h4=h4, node_type=node_type, index=index)

    def is_heading(self) -> bool:
        return not self.node_type

    def heading_level(self) -> int:
        """Return the deepest non-zero heading level (1-4)."""
        if self.h4:
            return 4
        if self.h3:
            return 3
        if self.h2:
            return 2
        if self.h1:
            return 1
        return 0

    def is_child_of(self, other: Path) -> bool:
        """Check if this path is a child of another path."""
        if other.h1 and self.h1 != other.h1:
            return False
        if other.h2 and self.h2 != other.h2:
            return False
        if other.h3 and self.h3 != other.h3:
            return False
        if other.h4 and self.h4 != other.h4:
            return False
        return True


class PathCounter:
    """Tracks heading counters and assigns paths to nodes."""

    def __init__(self):
        self.h1 = 0
        self.h2 = 0
        self.h3 = 0
        self.h4 = 0
        self._counters: dict[str, int] = {}

    def _section_key(self) -> str:
        return f"{self.h1}.{self.h2}.{self.h3}.{self.h4}"

    def next_heading(self, level: int) -> Path:
        """Advance heading counter and return the path."""
        if level == 1:
            self.h1 += 1
            self.h2 = 0
            self.h3 = 0
            self.h4 = 0
        elif level == 2:
            self.h2 += 1
            self.h3 = 0
            self.h4 = 0
        elif level == 3:
            self.h3 += 1
            self.h4 = 0
        elif level == 4:
            self.h4 += 1
        self._counters = {}
        return Path(h1=self.h1, h2=self.h2, h3=self.h3, h4=self.h4)

    def next_child(self, node_type: str) -> Path:
        """Get the next child path for a given node type."""
        key = self._section_key()
        type_key = f"{key}:{node_type}"
        count = self._counters.get(type_key, 0) + 1
        self._counters[type_key] = count
        return Path(
            h1=self.h1,
            h2=self.h2,
            h3=self.h3,
            h4=self.h4,
            node_type=node_type,
            index=count,
        )


# ---------------------------------------------------------------------------
# Node — a single element in any document
# ---------------------------------------------------------------------------


@dataclass
class Node:
    """A document node — heading, paragraph, table, figure, or equation."""

    slug: str
    path: Path
    node_type: str  # h, p, t, f, e, b
    text: str  # full content
    index: int = 0  # sequential position in document (0-indexed)
    precis: str = ""  # compressed summary (RAKE keywords or enrichment)
    page: int = 0  # source page number (if applicable)
    style: str = ""  # DOCX style name or LaTeX command
    source_file: str = ""  # LaTeX: which .tex file
    source_line_start: int = 0  # LaTeX: start line
    source_line_end: int = 0  # LaTeX: end line
    label: str = ""  # LaTeX: \label{} value
    comments: list[dict] = field(default_factory=list)  # [{id, author, text}]

    def heading_level(self) -> int:
        return self.path.heading_level()


# ---------------------------------------------------------------------------
# Handler — abstract base for all document types
# ---------------------------------------------------------------------------


@dataclass
class Plugin:
    """A precis plugin — declares corpus metadata + handler behavior.

    Plugins are the unit of packaging and discovery.  Each pip-installable
    precis extension (precis-papers, precis-todos, …) exposes one or more
    Plugin instances via the ``precis.plugins`` entry point group.

    File-based handlers (WordHandler, TexHandler) use ``file_types`` and
    leave ``corpus_id`` as None.  Corpus-based plugins (papers, todos, …)
    use ``schemes`` and set ``corpus_id``.

    Attributes:
        name: Plugin name used for logging and disable-list matching.
        handler_cls: The Handler subclass this plugin provides.
        schemes: URI schemes to register (e.g. ["paper", "doi", "arxiv"]).
        file_types: File extensions to register (e.g. [".docx"]).
        corpus_id: Corpus this plugin manages, or None for file handlers.
        write_policy: "ingestion" | "direct" | "system" — enforced by MCP.
        block_type_seeds: Extra (name, provenance, description) tuples.
        link_type_seeds: Extra (name, inverse, description) tuples.
    """

    name: str
    handler_cls: type[Handler]
    schemes: list[str] = field(default_factory=list)
    file_types: list[str] = field(default_factory=list)
    corpus_id: str | None = None
    write_policy: str = "ingestion"
    block_type_seeds: list[tuple] = field(default_factory=list)
    link_type_seeds: list[tuple] = field(default_factory=list)


class Handler(abc.ABC):
    """Base class for document type handlers.

    Subclass this to add support for a new document type (scheme or file
    extension). Implement ``read`` at minimum; override ``put`` to enable
    writing, and ``_write_note`` to enable annotations.
    """

    scheme: str = ""  # e.g. "file", "paper"
    writable: bool = False
    views: set[str] = set()  # supported /view names

    @abc.abstractmethod
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
        """Read/navigate/search document content."""
        ...

    def put(
        self,
        path: str,
        selector: str | None,
        text: str,
        mode: str,
        **kwargs,
    ) -> str:
        """Write to the document. Override in writable handlers."""
        if mode == "note":
            return self._write_note(path, selector, text, **kwargs)
        raise PrecisError(
            f"{self.scheme}: documents are read-only.\n"
            f"Supported: put(mode='note') to annotate."
        )

    def _write_note(
        self,
        path: str,
        selector: str | None,
        text: str,
        **kwargs,
    ) -> str:
        """Attach an annotation. Override per handler."""
        raise PrecisError(f"Annotations not supported for {self.scheme}:")
