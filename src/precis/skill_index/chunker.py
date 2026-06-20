"""Markdown chunker — one chunk per H2 section, with alias-group support.

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
- Every ``## …`` line opens a new chunk. The heading line is included
  in the chunk text so the embedder sees the section name as part of
  the signal.
- **Alias groups (v2):** when consecutive H2 headings appear with no
  body text between them, they form an *alias group*. Each heading
  in the group emits its own chunk; all chunks in the group share
  the body that follows the group. This is the v1 mechanism for
  multi-description-per-chunk (docs-and-skills-redesign decision 4):
  authors write multiple H2s that describe the same operation from
  different user angles; each angle embeds under its own heading.
- Empty groups (alias group at EOF with no body) are dropped.
- **Body-only twins (v3, opt-in):** with ``with_body_aliases=True``
  every section emits one extra ``body_only=True`` chunk carrying the
  section body *without* the heading line. The per-heading chunks fuse
  heading + body into one vector, which is great when the heading
  labels the body well and noise when it doesn't (``## Gotchas`` over
  a body about SSRF redirects). A heading-stripped vector de-noises
  that case. For an alias group the body is shared, so this is **one**
  extra chunk regardless of how many aliases. Body-only chunks are an
  *embedding-surface* concern only: they are NOT structural, so the
  ``slug~N`` chunk addresser and the TOC adapter both pass the default
  ``with_body_aliases=False`` and never see them. They are always
  appended *after* the structural chunks, keeping the structural
  prefix stable for callers that align by position.

Chunker version is bumped when the chunking strategy changes —
that invalidates the on-disk cache so old embeddings don't get
served against new chunk boundaries.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Bumped when the chunking strategy changes. Persisted in the
#: cache key path; old caches under a different version are
#: ignored (and prunable).
#:
#: - 1 → original H2 chunker, drops heading-only sections.
#: - 2 → adds alias-group support (consecutive H2s share body).
#: - 3 → adds opt-in body-only twin chunks (with_body_aliases).
CHUNKER_VERSION = 3


@dataclass(frozen=True)
class Chunk:
    """One H2 section of a markdown file.

    ``heading`` is the bare H2 text without the ``## `` prefix, or
    the empty string for the head chunk (content before the first
    H2). ``text`` is the full chunk body — heading line + body
    content — so the embedder sees the heading as part of the
    signal. In an alias group, multiple chunks share identical
    body text; they differ only by which alias heading prefixes
    that body.

    ``body_only`` marks a v3 body-only twin: ``text`` is the section
    body with the heading line stripped, emitted only when
    ``with_body_aliases=True``. These are an embedding-surface
    affordance, not a structural section — ``heading`` is still set
    (to the group's first alias) purely so a search hit on the twin
    has a sensible display anchor.
    """

    heading: str
    text: str
    body_only: bool = False


_FRONT_MATTER_RE = re.compile(r"^---\s*\n.*?\n---\s*\n", re.DOTALL)
_H2_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)


def _strip_front_matter(md: str) -> str:
    """Drop a leading ``---``-delimited YAML block, if present."""
    m = _FRONT_MATTER_RE.match(md)
    return md[m.end() :] if m else md


def chunk_by_h2(text: str, *, with_body_aliases: bool = False) -> list[Chunk]:
    """Split ``text`` into H2-section chunks with alias-group support.

    Returns an empty list when the input is empty or whitespace-only.
    For markdown without any ``## H2`` headings, returns a single
    chunk with empty heading containing the full body.

    Alias-group semantics: when two or more H2 headings appear with
    only whitespace between them, every heading in the group emits
    a chunk that shares the body following the group. If the group
    has no body following (alias group at EOF), it is dropped.

    When ``with_body_aliases`` is True, each section also emits one
    extra ``body_only=True`` chunk holding the section body with its
    heading line(s) stripped (one per *group*, since an alias group
    shares its body). These twins are appended after all structural
    chunks so callers that align by position can take the structural
    prefix. The default (False) returns only the structural chunks —
    used by the ``slug~N`` addresser and the TOC adapter.
    """
    body = _strip_front_matter(text).strip()
    if not body:
        return []

    matches = list(_H2_RE.finditer(body))
    if not matches:
        return [Chunk(heading="", text=body)]

    out: list[Chunk] = []
    # Body-only twins accumulate here and are appended after every
    # structural chunk, keeping the structural prefix stable.
    twins: list[Chunk] = []

    # Head chunk: everything before the first H2.
    head = body[: matches[0].start()].strip()
    if head:
        out.append(Chunk(heading="", text=head))

    # Walk matches, grouping consecutive H2s with only whitespace
    # between them into alias groups.
    n = len(matches)
    i = 0
    while i < n:
        group: list[re.Match[str]] = [matches[i]]

        # Extend the group while the next H2 follows the current one
        # with no non-whitespace content between.
        while i + 1 < n:
            cur_end = _line_end(body, matches[i].end())
            between = body[cur_end : matches[i + 1].start()]
            if between.strip():
                break
            i += 1
            group.append(matches[i])

        # Find the body shared by every heading in this group:
        # from the end of the last heading's line up to the next H2
        # start, or EOF.
        last = group[-1]
        body_start = _line_end(body, last.end())
        body_end = matches[i + 1].start() if i + 1 < n else len(body)
        shared_body = body[body_start:body_end].strip()

        if shared_body:
            for m in group:
                heading_text = m.group(1).strip()
                chunk_text = f"## {heading_text}\n{shared_body}"
                out.append(Chunk(heading=heading_text, text=chunk_text))
            if with_body_aliases:
                # One heading-stripped twin per group; the first
                # alias supplies the display anchor.
                twins.append(
                    Chunk(
                        heading=group[0].group(1).strip(),
                        text=shared_body,
                        body_only=True,
                    )
                )
        # else: alias group at EOF with no body — drop.

        i += 1

    out.extend(twins)
    return out


def _line_end(body: str, pos: int) -> int:
    """Return the index just past the newline ending the line at
    ``pos``, or ``len(body)`` if no newline.

    Used to step from the end of a regex match (the end of the
    heading text, before the trailing newline) onto the next line.
    """
    nl = body.find("\n", pos)
    return nl + 1 if nl >= 0 else len(body)
