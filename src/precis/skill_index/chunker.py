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
  every section emits one extra ``variant="body_only"`` chunk carrying
  the section body *without* the heading line. The per-heading chunks
  fuse heading + body into one vector, which is great when the heading
  labels the body well and noise when it doesn't (``## Gotchas`` over
  a body about SSRF redirects). A heading-stripped vector de-noises
  that case. For an alias group the body is shared, so this is **one**
  extra chunk regardless of how many aliases.
- **Heading-only twins (v4, opt-in):** same flag, one extra
  ``variant="heading_only"`` chunk *per alias heading* (not per group
  — unlike body-only, each alias gets its own), text = the bare
  heading with no body. Short intent queries ("check citations") match
  the heading signal without body dilution.
- **Question-only twins (v4, opt-in):** same flag, one chunk per
  question the file's front matter declares it answers — the
  ``summary:`` scalar plus each entry of an ``answers:`` list (see
  ``_extract_front_matter_questions``). Not tied to any H2 section;
  embedded standalone so an agent's how-do-I phrasing matches
  question-to-question instead of question-to-prose. Zero chunks when
  the front matter carries neither field.

Body-only / heading-only / question-only chunks are all an
*embedding-surface* concern, not structural: the ``slug~N`` chunk
addresser and the TOC adapter both pass the default
``with_body_aliases=False`` and never see them. They are always
appended *after* the structural chunks, keeping the structural
prefix stable for callers that align by position — see
:attr:`Chunk.variant` / :attr:`Chunk.body_only`.

Chunker version is bumped when the chunking strategy changes —
that invalidates the on-disk cache so old embeddings don't get
served against new chunk boundaries.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

#: Bumped when the chunking strategy changes. Persisted in the
#: cache key path; old caches under a different version are
#: ignored (and prunable).
#:
#: - 1 → original H2 chunker, drops heading-only sections.
#: - 2 → adds alias-group support (consecutive H2s share body).
#: - 3 → adds opt-in body-only twin chunks (with_body_aliases).
#: - 4 → adds opt-in heading-only and question-only twin chunks
#:   (same ``with_body_aliases`` flag; question-only pulls front-matter
#:   ``summary:``/``answers:``).
CHUNKER_VERSION = 4

#: Every shape a :class:`Chunk` can carry — see :attr:`Chunk.variant`.
CHUNK_VARIANTS: Final[frozenset[str]] = frozenset(
    {"structural", "body_only", "heading_only", "question_only"}
)


@dataclass(frozen=True)
class Chunk:
    """One H2 section of a markdown file, or a v3/v4 embedding twin.

    ``heading`` is the bare H2 text without the ``## `` prefix, or
    the empty string for the head chunk (content before the first
    H2) and for ``question_only`` twins (no single heading applies).
    ``text`` is the chunk's embedding surface — for ``"structural"``
    that's the heading line + body content, so the embedder sees the
    heading as part of the signal. In an alias group, multiple
    structural chunks share identical body text; they differ only by
    which alias heading prefixes that body.

    ``variant`` (see :data:`CHUNK_VARIANTS`) is one of:

    - ``"structural"`` (default) — the per-heading chunk described
      above; the only variant returned when ``with_body_aliases=False``.
    - ``"body_only"`` (v3) — the section body with the heading line
      stripped, one per alias *group* (shared body → one twin).
      ``heading`` is set to the group's first alias purely so a hit
      on the twin has a sensible display anchor.
    - ``"heading_only"`` (v4) — the bare heading text alone (``text``
      == ``heading``), one per alias heading (not per group).
    - ``"question_only"`` (v4) — one front-matter-derived question
      (``summary:`` or an ``answers:`` entry); ``heading`` is ``""``.

    ``body_only`` (the property below) is a back-compat boolean view:
    True for every variant except ``"structural"``. Every caller that
    used to filter "is this an embedding-surface twin, not a real
    section" keeps working unchanged for the new variants too.
    """

    heading: str
    text: str
    variant: str = "structural"

    @property
    def body_only(self) -> bool:
        return self.variant != "structural"


_FRONT_MATTER_RE = re.compile(r"^---\s*\n.*?\n---\s*\n", re.DOTALL)
_H2_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
_FM_SUMMARY_RE = re.compile(r"^summary:\s*(.+?)\s*$", re.MULTILINE)
_FM_ANSWERS_KEY_RE = re.compile(r"^answers:[ \t]*(.*)$", re.MULTILINE)


def _strip_front_matter(md: str) -> str:
    """Drop a leading ``---``-delimited YAML block, if present."""
    m = _FRONT_MATTER_RE.match(md)
    return md[m.end() :] if m else md


def _extract_front_matter_questions(text: str) -> tuple[str | None, list[str]]:
    """Pull ``summary:`` and ``answers:`` out of leading front matter.

    Returns ``(summary, answers)``: ``summary`` is the front-matter
    ``summary:`` scalar (``None`` if absent or empty); ``answers`` is
    every question in an ``answers:`` list — block form (indented
    ``- item`` lines) or inline comma-separated — in file order
    (``[]`` if absent). ``(None, [])`` when there is no front matter
    at all.

    Deliberately self-contained rather than delegating to
    ``handlers._skill_common.parse_frontmatter``: the chunker is a
    generic markdown-corpus tool (see module docstring) and
    ``summary:``/``answers:`` are the only two fields it needs, not
    the full skill front-matter schema.
    """
    m = _FRONT_MATTER_RE.match(text)
    if not m:
        return None, []
    fm = text[: m.end()]

    summary: str | None = None
    sm = _FM_SUMMARY_RE.search(fm)
    if sm:
        summary = sm.group(1).strip("\"'") or None

    answers: list[str] = []
    am = _FM_ANSWERS_KEY_RE.search(fm)
    if am:
        inline = am.group(1).strip()
        if inline:
            answers = [a.strip().strip("\"'") for a in inline.split(",") if a.strip()]
        else:
            # Block list: indented ``- item`` lines following the key,
            # up to the first line that isn't one (another key, or the
            # closing ``---``).
            for line in fm[am.end() :].splitlines():
                stripped = line.strip()
                if not stripped:
                    continue
                if not stripped.startswith("- "):
                    break
                item = stripped[2:].strip().strip("\"'")
                if item:
                    answers.append(item)
    return summary, answers


def chunk_by_h2(text: str, *, with_body_aliases: bool = False) -> list[Chunk]:
    """Split ``text`` into H2-section chunks with alias-group support.

    Returns an empty list when the input is empty or whitespace-only.
    For markdown without any ``## H2`` headings, returns a single
    chunk with empty heading containing the full body (plus any
    ``question_only`` twins, per ``with_body_aliases`` below).

    Alias-group semantics: when two or more H2 headings appear with
    only whitespace between them, every heading in the group emits
    a chunk that shares the body following the group. If the group
    has no body following (alias group at EOF), it is dropped (and so
    is its ``heading_only`` twin).

    When ``with_body_aliases`` is True, each section also emits:

    - one extra ``variant="body_only"`` chunk per *group* holding the
      section body with its heading line(s) stripped (an alias group
      shares its body, so this is one twin regardless of alias count);
    - one extra ``variant="heading_only"`` chunk *per alias heading*
      (unlike body-only, not per group) holding the bare heading text;
    - one ``variant="question_only"`` chunk per question the file's
      front matter declares — its ``summary:`` scalar, then each
      ``answers:`` entry (see :func:`_extract_front_matter_questions`)
      — appended once for the whole file, not per section.

    These twins are appended after all structural chunks (body_only,
    then heading_only, then question_only) so callers that align by
    position can take the structural prefix. The default (False)
    returns only the structural chunks — used by the ``slug~N``
    addresser and the TOC adapter.
    """
    body = _strip_front_matter(text).strip()
    if not body:
        return []

    question_twins: list[Chunk] = []
    if with_body_aliases:
        summary, answers = _extract_front_matter_questions(text)
        question_texts = ([summary] if summary else []) + answers
        question_twins = [
            Chunk(heading="", text=q, variant="question_only") for q in question_texts
        ]

    matches = list(_H2_RE.finditer(body))
    if not matches:
        return [Chunk(heading="", text=body), *question_twins]

    out: list[Chunk] = []
    # Body-only / heading-only twins accumulate here and are appended
    # after every structural chunk, keeping the structural prefix
    # stable.
    twins: list[Chunk] = []
    heading_twins: list[Chunk] = []

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
                    heading_twins.append(
                        Chunk(
                            heading=heading_text,
                            text=heading_text,
                            variant="heading_only",
                        )
                    )
            if with_body_aliases:
                # One heading-stripped twin per group; the first
                # alias supplies the display anchor.
                twins.append(
                    Chunk(
                        heading=group[0].group(1).strip(),
                        text=shared_body,
                        variant="body_only",
                    )
                )
        # else: alias group at EOF with no body — drop (both the
        # structural chunks and their heading_only twins).

        i += 1

    out.extend(twins)
    out.extend(heading_twins)
    out.extend(question_twins)
    return out


def _line_end(body: str, pos: int) -> int:
    """Return the index just past the newline ending the line at
    ``pos``, or ``len(body)`` if no newline.

    Used to step from the end of a regex match (the end of the
    heading text, before the trailing newline) onto the next line.
    """
    nl = body.find("\n", pos)
    return nl + 1 if nl >= 0 else len(body)
