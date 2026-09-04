"""Template-include preprocessor for shipped skill / doc markdown.

Resolves directives of the form ``{{include <source>:<slug>[#<section>]}}``
at ingest time, before chunking. Two sources defined in v1 (see
``docs/backlog/docs-and-skills-redesign.md`` decision 10):

- ``{{include doc:precis-common#address-grammar}}`` — content include
  from another shipped skill file. The section selector (``#…``) is
  the slugified text of an H2 heading inside the source file.
- ``{{include schema:put#arguments}}`` — code include; the resolver
  introspects the verb's signature + docstring in ``tools/core.py``
  (or kindspec) and emits a markdown table.

Each substitution is the resolved body verbatim — no surrounding
markers or comment fences. Agents reading the expanded skill see
the resolved content as if it had been authored in-place, which is
the right shape for LLM consumption (every extra byte is
context-window pressure).

The preprocessor itself is source-agnostic: it parses the directive,
looks up a resolver registered under the source name, and substitutes.
Production code wires :class:`DocResolver` and a schema resolver
(future) onto an :class:`Includer`; tests pass mock resolvers.

**Single-level expansion only.** Resolved content is not re-scanned
for further ``{{include …}}`` directives. If transitive expansion
becomes needed (e.g., precis-common fragments embedding schema
includes), lift this by re-running the expander on each resolver's
output — but the cycle-detection cost is real, so this is opt-in.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

#: ``{{include source:slug[#section]}}`` — section optional.
#: Source: ``[a-z]+`` keeps the surface intentionally narrow (we
#: don't want directive authors inventing new source kinds without
#: matching resolver support).
_INCLUDE_RE: Final[re.Pattern[str]] = re.compile(
    r"\{\{include\s+([a-z]+):([a-zA-Z0-9_-]+)(?:#([a-zA-Z0-9_-]+))?\s*\}\}"
)


class IncludeError(ValueError):
    """Raised by the preprocessor when a directive cannot be resolved.

    Ingest treats this as a hard-fail static gate (decision 10): the
    skill does not ingest until the include resolves. The previous
    version (if any) stays live.
    """


@dataclass(frozen=True)
class IncludeDirective:
    """A single parsed ``{{include …}}`` occurrence."""

    source: str
    slug: str
    section: str | None
    span: tuple[int, int]  # (start, end) char offsets in the source text

    def label(self) -> str:
        """Canonical short label used in the marker comments."""
        if self.section:
            return f"{self.source}:{self.slug}#{self.section}"
        return f"{self.source}:{self.slug}"


#: Resolver type: takes the slug + optional section, returns the
#: resolved markdown body. Resolver raises :class:`IncludeError` when
#: the target can't be found.
Resolver = Callable[[str, str | None], str]


@dataclass
class Includer:
    """Expands ``{{include …}}`` directives against registered resolvers.

    Pass a mapping ``{source_name: resolver_callable}`` — production
    wires ``{"doc": DocResolver(skills_root), "schema": …}``; tests
    pass small lambdas.
    """

    resolvers: dict[str, Resolver]

    def expand(self, text: str) -> str:
        """Return ``text`` with every ``{{include …}}`` expanded.

        Idempotent on input with no directives. Single-level only —
        the resolver's output is inserted verbatim and not re-scanned.
        """
        directives = list(parse_directives(text))
        if not directives:
            return text

        # Walk the directives in reverse so earlier spans aren't
        # invalidated by later substitutions widening the string.
        out = text
        for d in reversed(directives):
            resolver = self.resolvers.get(d.source)
            if resolver is None:
                raise IncludeError(
                    f"no resolver registered for source {d.source!r} "
                    f"(directive {d.label()})"
                )
            try:
                body = resolver(d.slug, d.section)
            except IncludeError:
                raise
            except Exception as exc:
                raise IncludeError(f"resolver for {d.label()} failed: {exc}") from exc

            replacement = body.rstrip()
            start, end = d.span
            out = out[:start] + replacement + out[end:]
        return out


def parse_directives(text: str) -> list[IncludeDirective]:
    """Return every ``{{include …}}`` directive in ``text``, in order.

    Directives inside fenced code blocks (``` ``` `` or ``~~~``, with
    or without an info string) or inline code spans (single
    backticks) are **not** treated as live directives — they're
    markdown showing an author the include syntax, not an include to
    resolve. See ``precis-common-reviewer.md`` for the motivating
    example: it teaches personas the ``{{include …}}`` syntax inside
    a fenced block, and those examples happen to be syntactically
    valid (and even self-referential) directives that must not
    actually expand.
    """
    literal_spans = _code_spans(text)
    out: list[IncludeDirective] = []
    for m in _INCLUDE_RE.finditer(text):
        if _in_any_span(m.start(), m.end(), literal_spans):
            continue
        out.append(
            IncludeDirective(
                source=m.group(1),
                slug=m.group(2),
                section=m.group(3),
                span=m.span(),
            )
        )
    return out


_FENCE_LINE_RE: Final[re.Pattern[str]] = re.compile(r"^\s*(`{3,}|~{3,})")
_INLINE_CODE_RE: Final[re.Pattern[str]] = re.compile(r"`[^`\n]+`")


def _code_spans(text: str) -> list[tuple[int, int]]:
    """Return char spans of fenced code blocks and inline code spans.

    Used to keep the include-directive scan from firing on markdown
    that merely *shows* the ``{{include …}}`` syntax rather than
    using it.
    """
    spans = _fenced_block_spans(text)
    # Inline code spans only matter outside fenced blocks (a fence's
    # interior is already excluded wholesale).
    for m in _INLINE_CODE_RE.finditer(text):
        if not _in_any_span(m.start(), m.end(), spans):
            spans.append(m.span())
    return spans


def _fenced_block_spans(text: str) -> list[tuple[int, int]]:
    """Return char spans covering ```/~~~ fenced code blocks.

    A fence opens with a line of 3+ backticks or tildes (optionally
    preceded by whitespace, optionally followed by an info string)
    and closes with a line carrying at least as many of the same
    fence character and nothing else. An unterminated fence runs to
    end-of-text — better to over-exclude than to expand inside a
    broken fence.
    """
    spans: list[tuple[int, int]] = []
    fence_char: str | None = None
    fence_len = 0
    fence_start = 0
    offset = 0
    for line in text.splitlines(keepends=True):
        m = _FENCE_LINE_RE.match(line)
        if fence_char is None:
            if m:
                fence_char = m.group(1)[0]
                fence_len = len(m.group(1))
                fence_start = offset
        else:
            if (
                m
                and m.group(1)[0] == fence_char
                and len(m.group(1)) >= fence_len
                and line[m.end() :].strip() == ""
            ):
                spans.append((fence_start, offset + len(line)))
                fence_char = None
        offset += len(line)
    if fence_char is not None:
        spans.append((fence_start, len(text)))
    return spans


def _in_any_span(start: int, end: int, spans: list[tuple[int, int]]) -> bool:
    return any(s <= start and end <= e for s, e in spans)


# ─────────────────────────────────────────────────────────────────────
# Built-in doc resolver
# ─────────────────────────────────────────────────────────────────────


@dataclass
class DocResolver:
    """Resolve ``doc:<slug>#<section>`` against an in-memory map of
    slug → full markdown body.

    The map is loaded once at ingest time (the boot-time scanner
    walks ``data/skills/*.md`` and feeds the body for each slug);
    passing it in keeps this resolver pure and easy to test.

    ``section`` is matched against the slugified text of each H2
    inside the target file (see :func:`slugify_heading`). When
    ``section`` is ``None``, the entire file body (after frontmatter
    is stripped) is returned.
    """

    docs: dict[str, str]

    def __call__(self, slug: str, section: str | None) -> str:
        body = self.docs.get(slug)
        if body is None:
            raise IncludeError(f"doc include: unknown slug {slug!r}")

        # Drop YAML frontmatter so includes don't leak it.
        body = _strip_frontmatter(body)

        if section is None:
            return body

        match = _find_section(body, section)
        if match is None:
            raise IncludeError(
                f"doc include: section {section!r} not found in {slug!r}"
            )
        return match


def slugify_heading(text: str) -> str:
    """Lower-case, hyphenate, strip punctuation. Stable across edits
    that don't change the heading's word content.

    Matches the GitHub-style anchor convention closely enough to be
    intuitive for authors who think in terms of URL fragments.
    """
    text = text.strip().lower()
    # Drop anything that isn't alnum or whitespace; collapse to hyphens.
    text = re.sub(r"[^a-z0-9\s-]+", "", text)
    text = re.sub(r"[\s-]+", "-", text)
    return text.strip("-")


def _strip_frontmatter(text: str) -> str:
    if not text.startswith("---"):
        return text
    end = text.find("\n---", 3)
    if end == -1:
        return text
    # Skip past the closing ``---`` line.
    after = text[end + 4 :]
    return after.lstrip("\n")


_H2_RE: Final[re.Pattern[str]] = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)


def _find_section(body: str, section_slug: str) -> str | None:
    """Return the body of the H2 whose slugified heading matches
    ``section_slug``, or None if no such H2 exists.

    The section body is everything from the H2's start through the
    end-of-file or the next H1/H2 (whichever comes first).
    """
    matches = list(_H2_RE.finditer(body))
    for i, m in enumerate(matches):
        if slugify_heading(m.group(1)) != section_slug:
            continue
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        # Also stop at the next H1 if one appears before the next H2
        # — pragmatic for files that mix levels. Start the search
        # past the current heading line so we don't false-match the
        # H2 we're inside.
        heading_end = body.find("\n", start)
        if 0 <= heading_end < end:
            h1_match = re.search(r"^#\s+", body[heading_end + 1 : end], re.MULTILINE)
            if h1_match:
                end = heading_end + 1 + h1_match.start()
        return body[start:end].rstrip()
    return None
