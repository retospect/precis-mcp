"""Node model, slug generation, and path formatting."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Optional

# Base34 alphabet — no I, O to avoid ambiguity
SLUG_CHARS = "0123456789ABCDEFGHJKLMNPQRSTUVWXYZ"
SLUG_LEN = 5

# Path regex: S{h1}[.{h2}[.{h3}[.{h4}]]][{type}{n}]
PATH_RE = re.compile(r"^S(\d+)(?:\.(\d+)(?:\.(\d+)(?:\.(\d+))?)?)?(?:([ptfeb¶])(\d+))?$")

# Display mapping: internal node_type char → display char
_TYPE_DISPLAY = {"p": "¶"}
_TYPE_INTERNAL = {"¶": "p"}  # inverse for parsing


def make_slug(text: str) -> str:
    """Generate a 5-char base34 content slug from text."""
    h = int(hashlib.sha256(text.strip().encode()).hexdigest()[:8], 16)
    out = []
    for _ in range(SLUG_LEN):
        h, r = divmod(h, len(SLUG_CHARS))
        out.append(SLUG_CHARS[r])
    return "".join(out)


def resolve_slug(slug: str, slug_counts: dict[str, int]) -> str:
    """Resolve a slug with collision suffix if needed.

    slug_counts tracks how many times each base slug has been seen.
    Returns slug with .2, .3 suffix for duplicates.
    """
    count = slug_counts.get(slug, 0) + 1
    slug_counts[slug] = count
    if count == 1:
        return slug
    return f"{slug}.{count}"


@dataclass
class Path:
    """Positional heading-path ID."""

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
        """Parse a path string like S1.2p3 or S1.2.0.0p3."""
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
        """Return the deepest non-zero heading level (1-4), or 0 for preamble."""
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
        """Check if this path is a child of another path (heading scope)."""
        if other.h1 and self.h1 != other.h1:
            return False
        if other.h2 and self.h2 != other.h2:
            return False
        if other.h3 and self.h3 != other.h3:
            return False
        if other.h4 and self.h4 != other.h4:
            return False
        return True

    def starts_with(self, prefix: str) -> bool:
        """Check if string representation starts with prefix."""
        return str(self).startswith(prefix)


def _source_loc(source_file: str, start: int, end: int) -> str:
    """Format source location as file:start..end or file:start."""
    if end > start:
        return f"{source_file}:{start}..{end}"
    return f"{source_file}:{start}"


@dataclass
class Node:
    """A document node — heading, paragraph, table, figure, or equation."""

    slug: str
    path: Path
    node_type: str  # h, p, t, f, e, b
    text: str  # full content
    precis: str = ""  # compressed summary (plain text)
    style: str = ""  # DOCX style name or LaTeX command
    source_file: str = ""  # LaTeX: which .tex file
    source_line_start: int = 0  # LaTeX: start line
    source_line_end: int = 0  # LaTeX: end line
    label: str = ""  # LaTeX: \label{} value
    comments: list[dict] = field(default_factory=list)  # [{id, author, text}]

    def heading_level(self) -> int:
        return self.path.heading_level()

    def toc_line(self, max_width: int = 120) -> str:
        """Format as a single toc line.

        Headings:  S1    KR8M2  methods.tex:24..99  #| Introduction
        Content:   S1p1  HU73F  methods.tex:30..45  |  phrase; phrase; phrase
        """
        path_str = str(self.path)
        ctag = f" 💬{len(self.comments)}" if self.comments else ""
        if self.node_type == "h":
            level = self.heading_level()
            hashes = "#" * level if level else ""
            if self.source_file:
                loc = _source_loc(
                    self.source_file, self.source_line_start, self.source_line_end
                )
                line = f"{path_str}  {self.slug}{ctag}  {loc}  {hashes}| {self.text}"
            else:
                line = f"{path_str}  {self.slug}{ctag}  {hashes}| {self.text}"
        else:
            display = self.precis or self.text
            if self.source_file:
                loc = _source_loc(
                    self.source_file, self.source_line_start, self.source_line_end
                )
                line = f"{path_str}  {self.slug}{ctag}  {loc}  |  {display}"
            else:
                line = f"{path_str}  {self.slug}{ctag}  |  {display}"

        if len(line) > max_width:
            line = line[: max_width - 1] + "…"

        return line

    def meta_line(self) -> str:
        """Format as a >> metadata line for get() output."""
        parts = [self.slug, str(self.path)]
        if self.source_file:
            loc = f"{self.source_file}:{self.source_line_start}"
            if self.source_line_end > self.source_line_start:
                loc = f"{self.source_file}:{self.source_line_start}..{self.source_line_end}"
            parts.append(loc)
        if self.node_type == "h":
            parts.append(self.text)
            return ">> " + " ".join(parts)
        else:
            precis = self.precis or self.text
            return ">> " + " ".join(parts) + ": " + precis

    def grep_line(self) -> str:
        """Format as a grep result line."""
        precis = self.precis or self.text
        return f"{self.path} {self.slug}: {precis}"


class PathCounter:
    """Tracks heading counters and assigns paths to nodes."""

    def __init__(self):
        self.h1 = 0
        self.h2 = 0
        self.h3 = 0
        self.h4 = 0
        self._counters: dict[str, int] = {}  # type counters per section

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
