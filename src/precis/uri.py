"""URI parser for precis v2.

Grammar::

    uri       := scheme ":" path [ "~" selector ] [ "/" view [ "/" subview ] ]
    scheme    := "file" | "paper" | ...
    path      := identifier (slug, filename, etc.)
    selector  := slug | index | path_ref | range | context_window
    view      := "toc" | "meta" | "abstract" | "cite" | "cites" | "cited-by" | ...
    subview   := "bib" | "acs" | "apa" | "ris" | ...

Selector separator is ``~`` (tilde).

Selector patterns (disambiguated by regex)::

    [A-Z0-9]{5}             content slug        ~KR8M2
    S\\d+[.\\d+]*[¶\\d+]?   hierarchical path   ~S1.2¶3
    \\d+                     block index         ~38

Ranges::

    ~38..42                 absolute range
    ~38..                   open range (paginated)
    ~SLUG-3..+3             relative context window

Examples::

    paper:                              list library
    paper:miller2023foo                 overview
    paper:miller2023foo/toc             table of contents
    paper:miller2023foo~38              chunk 38
    paper:miller2023foo~38..42          chunks 38-42
    paper:miller2023foo~KR8M2-3..+3     context around slug
    paper:miller2023foo/cite/bib        BibTeX citation
    paper:miller2023foo/cites           outgoing references
    file:planning.docx                  toc
    file:planning.docx~ABCDE           node by slug
    file:planning.docx~S1.2            section by path
    file:main.tex~sec:methods          node by LaTeX label
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

# Schemes where / is part of the identifier (not a view separator)
_OPAQUE_PATH_SCHEMES = {"doi", "arxiv", "usc", "irs", "ie"}

# Selector patterns
_SLUG_RE = re.compile(r"^[A-Z0-9]{5}(?:\.\d+)?$")
_PATH_RE = re.compile(r"^S\d+")
_INDEX_RE = re.compile(r"^\d+$")

# Context window: ANCHOR-N..+M or ANCHOR..+M or ANCHOR-N..
_CONTEXT_RE = re.compile(
    r"^(.+?)"  # anchor (greedy-minimal)
    r"(?:-(\d+))?"  # optional -N (before count)
    r"\.\."  # ..
    r"(?:\+(\d+))?$"  # optional +M (after count)
)

# Absolute range: START..END or START..
_RANGE_RE = re.compile(r"^(\d+)\.\.(\d*)$")


@dataclass
class ParsedURI:
    """Result of parsing a precis URI."""

    scheme: str  # "file", "paper", etc.
    path: str  # document identifier (filename, slug, empty for bare scheme)
    selector: str | None = None  # raw selector string (after ~)
    view: str | None = None  # /view
    subview: str | None = None  # /view/subview
    raw: str = ""  # original URI string

    # Parsed selector details (populated by resolve_selector)
    selector_type: str = ""  # "slug", "index", "path", "label", ""
    anchor: str | None = None  # resolved anchor (slug, index str, path str)
    range_start: Optional[int] = None  # absolute range start
    range_end: Optional[int] = None  # absolute range end (None = open)
    context_before: Optional[int] = None  # -N in context window
    context_after: Optional[int] = None  # +M in context window

    @property
    def is_bare(self) -> bool:
        """True if no path given (e.g. 'paper:')."""
        return not self.path

    @property
    def has_range(self) -> bool:
        return self.range_start is not None

    @property
    def has_context(self) -> bool:
        return self.context_before is not None or self.context_after is not None

    @property
    def is_open_range(self) -> bool:
        return self.range_start is not None and self.range_end is None


def parse(uri: str) -> ParsedURI:
    """Parse a precis URI string into its components.

    Raises ValueError if the URI has no valid scheme.
    """
    raw = uri.strip()

    # Split scheme:rest
    colon = raw.find(":")
    if colon < 1:
        raise ValueError(f"Invalid URI (no scheme): {uri!r}")

    scheme = raw[:colon].lower()
    rest = raw[colon + 1:]

    # Split off ~selector
    selector = None
    tilde_pos = rest.find("~")
    if tilde_pos >= 0:
        selector = rest[tilde_pos + 1:]
        rest = rest[:tilde_pos]
        # Selector might contain /view — split at first / after the selector core
        # But only if / comes after range syntax is done
        sel_slash = _find_view_slash_in_selector(selector)
        if sel_slash >= 0:
            # Move /view part back to rest
            rest = rest + selector[sel_slash:]
            selector = selector[:sel_slash]

    # Split off /view[/subview]
    # For opaque-path schemes (doi, arxiv) slashes are part of the identifier
    view = None
    subview = None
    if scheme in _OPAQUE_PATH_SCHEMES:
        path = rest  # entire rest is the identifier, no view splitting
    elif "/" in rest:
        parts = rest.split("/")
        path = parts[0]
        if len(parts) >= 2 and parts[1]:
            view = parts[1]
        if len(parts) >= 3 and parts[2]:
            subview = "/".join(parts[2:])
    else:
        path = rest

    parsed = ParsedURI(
        scheme=scheme,
        path=path,
        selector=selector if selector else None,
        view=view,
        subview=subview,
        raw=raw,
    )

    # Resolve selector details
    if selector:
        _resolve_selector(parsed, selector)

    return parsed


def _find_view_slash_in_selector(selector: str) -> int:
    """Find the position of a /view slash within a selector string.

    Must distinguish /view from range syntax. A / after digits/dots/+
    that aren't part of a range is a view slash.
    """
    # If selector contains .., everything before the first / after .. is range
    dot_dot = selector.find("..")
    if dot_dot >= 0:
        # Find / after the range part
        after_range = selector.find("/", dot_dot)
        if after_range >= 0:
            return after_range
        return -1

    # No range — first / is a view slash
    slash = selector.find("/")
    return slash


def _resolve_selector(parsed: ParsedURI, selector: str) -> None:
    """Classify and parse the selector into the ParsedURI fields."""
    # Check for context window: ANCHOR-N..+M
    ctx = _CONTEXT_RE.match(selector)
    if ctx:
        anchor = ctx.group(1)
        before = int(ctx.group(2)) if ctx.group(2) else None
        after = int(ctx.group(3)) if ctx.group(3) else None

        # If anchor is pure digits, also check if this is an absolute range
        if _INDEX_RE.match(anchor) and before is None and after is None:
            # This is actually an absolute range: 38..42 or 38..
            rng = _RANGE_RE.match(selector)
            if rng:
                parsed.range_start = int(rng.group(1))
                parsed.range_end = int(rng.group(2)) if rng.group(2) else None
                parsed.selector_type = "index"
                parsed.anchor = rng.group(1)
                return

        _classify_anchor(parsed, anchor)
        parsed.context_before = before
        parsed.context_after = after
        return

    # Check for absolute range: 38..42, 38..
    rng = _RANGE_RE.match(selector)
    if rng:
        parsed.range_start = int(rng.group(1))
        parsed.range_end = int(rng.group(2)) if rng.group(2) else None
        parsed.selector_type = "index"
        parsed.anchor = rng.group(1)
        return

    # Simple selector (no range, no context)
    _classify_anchor(parsed, selector)


def _classify_anchor(parsed: ParsedURI, anchor: str) -> None:
    """Classify a bare anchor string into slug, index, path, or label."""
    parsed.anchor = anchor

    if _SLUG_RE.match(anchor):
        parsed.selector_type = "slug"
    elif _INDEX_RE.match(anchor):
        parsed.selector_type = "index"
        # Single index: also set range_start = range_end for uniform access
        parsed.range_start = int(anchor)
        parsed.range_end = int(anchor)
    elif _PATH_RE.match(anchor):
        parsed.selector_type = "path"
    else:
        # Fallback: could be a LaTeX label or other handler-specific ID
        parsed.selector_type = "label"
