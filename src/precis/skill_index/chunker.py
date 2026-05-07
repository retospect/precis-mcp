"""Markdown chunker — one chunk per H2 section.

A skill is typically organised as ``# H1`` (title), then a series of
``## H2`` sections (Verbs / Examples / See also / …). Embedding the
whole file as one vector smears every section's signal together; a
query for "callgraph depth" against the full ``precis-python-help``
loses to noise. One vector per H2 section keeps the per-concept
signal sharp and gives the search response a natural display anchor
(the section heading).

Strategy:

- Skip optional YAML front-matter delimited by ``---``.
- The text *before* the first H2 (typically the H1 + intro paragraph)
  is its own "head" chunk, so a skill with no H2s still indexes.
- Every subsequent ``## ...`` line opens a new chunk; the heading
  line is included in the chunk text so the embedder sees it.
- Empty chunks (heading with nothing under it) are dropped.

Chunker version is bumped when the chunking strategy changes —
that invalidates the on-disk cache so old embeddings don't get
served against new chunk boundaries.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Bumped when the chunking strategy changes. Persisted in the
#: cache key path; old caches under a different version are
#: ignored (and prunable). Start at 1.
CHUNKER_VERSION = 1


@dataclass(frozen=True)
class Chunk:
    """One H2 section of a markdown file.

    ``heading`` is the bare H2 text without the ``## `` prefix, or
    the empty string for the head chunk (content before the first
    H2). ``text`` is the full chunk body with the heading line
    included so the embedder gets the section name as part of the
    signal.
    """

    heading: str
    text: str


_FRONT_MATTER_RE = re.compile(r"^---\s*\n.*?\n---\s*\n", re.DOTALL)
_H2_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)


def _strip_front_matter(md: str) -> str:
    """Drop a leading ``---``-delimited YAML block, if present."""
    m = _FRONT_MATTER_RE.match(md)
    return md[m.end() :] if m else md


def chunk_by_h2(text: str) -> list[Chunk]:
    """Split ``text`` into H2-section chunks.

    Returns an empty list when the input is empty or whitespace-only.
    For markdown without any ``## H2`` headings, returns a single
    chunk with empty heading containing the full body.
    """
    body = _strip_front_matter(text).strip()
    if not body:
        return []

    matches = list(_H2_RE.finditer(body))
    if not matches:
        return [Chunk(heading="", text=body)]

    out: list[Chunk] = []

    # Head chunk: everything before the first H2.
    head = body[: matches[0].start()].strip()
    if head:
        out.append(Chunk(heading="", text=head))

    # Each H2 plus its body.
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        section = body[start:end].strip()
        # Section text begins with the H2 line itself; drop sections
        # whose body is just the heading with no content.
        if "\n" not in section:
            continue
        out.append(Chunk(heading=m.group(1).strip(), text=section))

    return out
