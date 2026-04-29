"""Hierarchical TOC rendering for papers — phase 3.5.

`acatome-extract` bundles preserve heading patterns from the source
PDF in their block text. We detect those patterns with a couple of
regexes and group consecutive blocks into sections + subsections.

Patterns (in priority order):

    H1: ``■ **NAME**``           — top-level section (RESULTS, METHODS …)
    H2: ``**Name**``             — subsection (single-line bold-only block)
    H1 (md): ``# Name``          — markdown fallback
    H2 (md): ``## Name``         — markdown fallback

Anything else is body. A heading block "owns" the body blocks after
it until the next heading at its level (or higher).

Output mirrors v1's structured TOC style — see the live diff in the
phase 3.5 plan. Range-scoped TOCs (``slug~46..105/toc``) call the
same renderer with a ``pos_filter`` so drill-down is recursive.

Pure logic — no DB, no IO. The Store hands us a list of ``Block``
objects; we slice + render.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from precis.store.types import Block
from precis.utils.next_block import format_next_block

# ---------------------------------------------------------------------------
# Heading detection
# ---------------------------------------------------------------------------

# H1: leading ■ marker + bold all-caps. Acatome-extract pattern; v1
# uses the exact same pattern for "section" detection.
_H1_RE = re.compile(r"^\s*■\s*\*\*([^*]+?)\*\*\s*$", re.UNICODE)

# H2: bold-only block, single line, starts with capital. The
# negative lookbehind for ■ avoids double-matching H1 lines.
_H2_RE = re.compile(r"^\s*\*\*([A-Z][^*]{0,80}?)\*\*\s*$")

# Markdown fallbacks — Wikipedia-style H1/H2 if a bundle was extracted
# from markdown rather than PDF. Keep the priority lower than the
# acatome patterns above.
_MD_H1_RE = re.compile(r"^\s*#\s+(\S.*?)\s*$")
_MD_H2_RE = re.compile(r"^\s*##\s+(\S.*?)\s*$")

# ---------------------------------------------------------------------------
# Metadata anti-patterns
# ---------------------------------------------------------------------------
#
# acatome-extract bundles emit publisher metadata blocks early in the
# stream as bold-only single lines — `**DOI: 10.1002/...**`,
# `**Keywords: photocatalysis, NOx**`, `**Received: 12 Mar 2024**`,
# etc. These match `_H2_RE` cleanly and were previously treated as
# H2 headings, so the first metadata block "owned" every body block
# until the next bold-only line. The MCP critic flagged a paper
# where 357 of 460 blocks landed under a single ``DOI:…`` pseudo-
# heading; the hierarchical TOC was useless on that ref.
#
# We reject any candidate H1/H2 whose title text matches one of the
# known publisher-metadata leads, contains a DOI string, contains a
# URL, or runs longer than the typical real subsection length.

# Metadata "lead" words — rejected as headings only when the title
# *also* carries a metadata shape signal (colon / em-dash / digit / @).
# This split keeps a real subsection like "DOI tracking subsection"
# (no colon, no digit) from being false-flagged just because it
# starts with "DOI".
_METADATA_LEAD_RE = re.compile(
    r"""^(?:
        DOI                |
        Keywords?          |
        Author(?:s|ship)?  |
        Affiliation(?:s)?  |
        Received           |
        Accepted           |
        Published          |
        Corresponding      |
        Email              |
        E-?mail            |
        ORCID              |
        Cite[\ ]this       |
        Submitted          |
        Revised
    )\b
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Shape signals — punctuation and content that's typical for
# publisher metadata but rare in section heading titles. ``:`` /
# em-dash / en-dash for ``DOI: …`` and ``Authors — …``; ``@`` for
# email; any digit for dates like ``Received 12 Mar 2024``.
_METADATA_SHAPE_RE = re.compile(r"[:\u2014\u2013@]|\d")

# Phrases that are always publisher metadata, with or without any
# trailing punctuation. These are unambiguous — no real subsection
# title in a paper will start with "Copyright" or "Article history".
# Each alternative is followed by ``(?!\w)`` (non-word lookahead)
# rather than ``\b`` so the ``©`` glyph (non-word) matches cleanly.
_METADATA_PHRASE_RE = re.compile(
    r"""^(?:
        ©                          |
        Copyright                  |
        License                    |
        Funding                    |
        Conflict[\ ]of[\ ]interest |
        Article[\ ]history         |
        Supplementary              |
        Available[\ ]online        |
        Cite[\ ]this[\ ]article
    )(?!\w)
    """,
    re.IGNORECASE | re.VERBOSE,
)

_DOI_IN_TITLE_RE = re.compile(r"\b10\.\d{4,9}/[^\s]+", re.IGNORECASE)
_URL_IN_TITLE_RE = re.compile(r"https?://", re.IGNORECASE)

# Real subsection titles are short. Anything longer than this in the
# H2 path is almost certainly a metadata sentence ("Received 12
# March 2024 and accepted on 14 May 2024.") rather than a heading.
# H1 has the ``■`` marker as a stronger gate, so the cap matters
# less there but we still apply it for safety.
_HEADING_TITLE_MAX_LEN = 60


def _is_metadata_title(title: str) -> bool:
    """Return True when ``title`` looks like publisher metadata.

    Anti-pattern hits:
      * always-metadata phrase (``©``, ``Copyright``, ``Article
        history``, …) — see :data:`_METADATA_PHRASE_RE`.
      * conditional-metadata lead (``DOI``, ``Keywords``, …) followed
        by colon or em-dash — see :data:`_METADATA_LEAD_COLON_RE`.
        The colon requirement avoids false-flagging a real subsection
        like "DOI tracking" that happens to start with a metadata
        word.
      * contains a DOI string anywhere.
      * contains a URL anywhere.
      * exceeds the title-length cap.

    Centralised so H1 and H2 detection share the same filter — we
    don't want a `■ **DOI: 10.x/y**` bundle artefact to slip through
    just because it carries the H1 marker.
    """
    if len(title) > _HEADING_TITLE_MAX_LEN:
        return True
    if _METADATA_PHRASE_RE.match(title):
        return True
    if _METADATA_LEAD_RE.match(title) and _METADATA_SHAPE_RE.search(title):
        return True
    if _DOI_IN_TITLE_RE.search(title):
        return True
    if _URL_IN_TITLE_RE.search(title):
        return True
    return False


@dataclass(frozen=True, slots=True)
class HeadingHit:
    """A detected heading."""

    pos: int  # block.pos
    title: str
    level: int  # 1 = section, 2 = subsection


def detect_heading(block: Block) -> HeadingHit | None:
    """Classify a block as H1 / H2 / not-a-heading.

    Multi-line blocks are never treated as headings — real headings
    are short single-line entries. Publisher metadata blocks (DOI,
    keywords, author affiliations, …) are rejected even when they
    match the bold-only pattern; see :func:`_is_metadata_title`.
    """
    text = block.text.strip()
    if not text or "\n" in text:
        return None

    candidates: tuple[tuple[re.Pattern[str], int], ...] = (
        (_H1_RE, 1),
        (_MD_H1_RE, 1),
        (_H2_RE, 2),
        (_MD_H2_RE, 2),
    )
    for pattern, level in candidates:
        m = pattern.match(text)
        if m is None:
            continue
        title = m.group(1).strip()
        if _is_metadata_title(title):
            return None
        return HeadingHit(pos=block.pos, title=title, level=level)
    return None


# ---------------------------------------------------------------------------
# Section grouping
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Section:
    """A contiguous run of blocks under one heading."""

    title: str
    """Heading text. Empty for the implicit "untitled" leading section."""

    level: int
    """1 = section, 2 = subsection. 0 = implicit (no heading)."""

    start: int
    """First ``block.pos`` covered, inclusive."""

    end: int
    """Last ``block.pos`` covered, inclusive."""

    children: tuple[Section, ...] = ()
    """Subsections under this section (level=2 under a level=1)."""

    @property
    def block_count(self) -> int:
        return self.end - self.start + 1


def build_toc(blocks: list[Block]) -> list[Section]:
    """Group ``blocks`` into a hierarchical TOC.

    Returns a list of top-level :class:`Section` objects. H2 headings
    nest under their preceding H1 (if any); H2s before any H1 land at
    the top level (no parent).

    An implicit leading "untitled" section is emitted only when the
    first block isn't a heading and there's content above the first
    real heading. This keeps the TOC complete (no orphan blocks) while
    still rendering cleanly when a paper starts with `■ **TITLE**`.
    """
    if not blocks:
        return []

    # Pass 1: identify all heading positions.
    headings: list[HeadingHit] = []
    for b in blocks:
        h = detect_heading(b)
        if h is not None:
            headings.append(h)

    if not headings:
        # No headings at all — return one implicit section spanning
        # everything. UI will fall back to a flat listing.
        return [
            Section(
                title="",
                level=0,
                start=blocks[0].pos,
                end=blocks[-1].pos,
            )
        ]

    # Pass 2: walk headings to build (start, end) ranges. The end of a
    # section is one before the next heading at the same-or-higher
    # level. We do this for level=1 first, then assign level=2s into
    # their parent section.
    last_pos = blocks[-1].pos

    # Untitled leading section, if any blocks precede the first heading.
    sections: list[Section] = []
    first_h_pos = headings[0].pos
    if first_h_pos > blocks[0].pos:
        sections.append(
            Section(
                title="",
                level=0,
                start=blocks[0].pos,
                end=first_h_pos - 1,
            )
        )

    # Pass 2a: H1 ranges. An H1 ends one before the next H1.
    h1s = [h for h in headings if h.level == 1]
    h1_ranges: list[tuple[HeadingHit, int]] = []
    for i, h in enumerate(h1s):
        end = (h1s[i + 1].pos - 1) if i + 1 < len(h1s) else last_pos
        h1_ranges.append((h, end))

    # Pass 2b: H2s nest into the H1 whose range contains them. H2s
    # before the first H1 (rare) sit at the top level on their own.
    def _collect_children(parent_start: int, parent_end: int) -> tuple[Section, ...]:
        """Build child Section list for H2s inside [parent_start, parent_end]."""
        children: list[Section] = []
        h2s_in = [
            h for h in headings if h.level == 2 and parent_start <= h.pos <= parent_end
        ]
        for j, h in enumerate(h2s_in):
            # Subsection ends at the next H2 - 1, or at parent_end.
            sub_end = (h2s_in[j + 1].pos - 1) if j + 1 < len(h2s_in) else parent_end
            children.append(
                Section(
                    title=h.title,
                    level=2,
                    start=h.pos,
                    end=sub_end,
                )
            )
        return tuple(children)

    # H2s before the first H1.
    if h1s and headings[0].level == 2:
        for h in headings:
            if h.level != 2 or h.pos >= h1s[0].pos:
                break
            sections.append(
                Section(
                    title=h.title,
                    level=2,
                    start=h.pos,
                    end=h1s[0].pos - 1,
                )
            )

    # H1s, with their nested H2 children.
    for h, end in h1_ranges:
        children = _collect_children(h.pos + 1, end)
        sections.append(
            Section(
                title=h.title,
                level=1,
                start=h.pos,
                end=end,
                children=children,
            )
        )

    # Edge case: only H2s, no H1s. Treat each H2 as a top-level section.
    if not h1s:
        h2s = [h for h in headings if h.level == 2]
        for j, h in enumerate(h2s):
            sub_end = (h2s[j + 1].pos - 1) if j + 1 < len(h2s) else last_pos
            sections.append(Section(title=h.title, level=2, start=h.pos, end=sub_end))

    return sections


# ---------------------------------------------------------------------------
# Filtering by range (drill-down)
# ---------------------------------------------------------------------------


def filter_toc_to_range(
    toc: list[Section],
    *,
    lo: int,
    hi: int,
) -> list[Section]:
    """Return a TOC restricted to sections that overlap ``[lo, hi]``.

    A section that's only partially inside the range is **clipped**
    (its ``start`` / ``end`` are pulled into the range). Children are
    likewise filtered + clipped. Empty sections are dropped.
    """

    def _clip(s: Section) -> Section | None:
        if s.end < lo or s.start > hi:
            return None
        new_start = max(s.start, lo)
        new_end = min(s.end, hi)
        clipped_children = tuple(c for c in (_clip(ch) for ch in s.children) if c)
        return Section(
            title=s.title,
            level=s.level,
            start=new_start,
            end=new_end,
            children=clipped_children,
        )

    return [s for s in (_clip(top) for top in toc) if s is not None]


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_toc(
    *,
    slug: str,
    toc: list[Section],
    total_blocks: int,
    blocks_by_pos: dict[int, Block] | None = None,
    range_label: str | None = None,
) -> str:
    """Render a hierarchical TOC as text.

    Args:
        slug:           paper slug (for header + drill-down hint calls)
        toc:            output of :func:`build_toc` (optionally clipped)
        total_blocks:   block count for the *whole* paper (header line)
        blocks_by_pos:  optional ``{pos: Block}`` lookup so we can
                        render a short preview from the *first body
                        block* of each section. When ``None``, sections
                        render with title only.
        range_label:    e.g. ``"~46..105"``; appears in the header to
                        signal a drilled-down view.

    Output style (matches v1 structured TOC)::

        # slug — TOC (177 blocks, 12 sections)
          ~0..7    (8)   <untitled overview>
          ~8..20   (13)  ■ INTRODUCTION
          ~21..40  (20)  ■ THEORY
          ~41..73  (33)  ■ METHODS
            ~43..53 (11)   Physics-Informed Program Synthesis [PIPS]
            ~54..58 (5)    Calculation Details
            ...
    """
    # Header
    n_sections = sum(_count_sections(s) for s in toc)
    rl = f" {range_label}" if range_label else ""
    lines = [
        f"# {slug} — TOC{rl} ({total_blocks} blocks, {n_sections} sections)",
        "",
    ]

    # Compute column widths so ranges line up.
    rows: list[tuple[int, str, str, str]] = []  # (depth, range, count, label)
    for s in toc:
        rows.extend(_collect_rows(s, depth=0, blocks_by_pos=blocks_by_pos))
    if not rows:
        lines.append("  (no sections in range)")
        return "\n".join(lines)

    range_w = max(len(r[1]) for r in rows)
    count_w = max(len(r[2]) for r in rows)

    for depth, rng, count, label in rows:
        indent = "  " + ("  " * depth)
        lines.append(f"{indent}{rng:<{range_w}} {count:<{count_w}}  {label}")

    # Drill-down hint trailer — only when there are at least 2 top-
    # level sections to drill into; otherwise a flat paper doesn't
    # benefit and the hint is noise.
    if len(toc) >= 2:
        biggest = max(
            toc,
            key=lambda s: s.block_count,
        )
        lines.append("")
        rl_call = f"~{biggest.start}..{biggest.end}"
        lines.append("Next:")
        lines.extend(
            format_next_block(
                [
                    (
                        f"get(kind='paper', id='{slug}{rl_call}/toc')",
                        f"drill into {biggest.title or 'the largest section'}",
                    ),
                    (
                        f"get(kind='paper', id='{slug}{rl_call}')",
                        f"read {biggest.title or 'the largest section'}",
                    ),
                    (
                        f"get(kind='paper', id='{slug}', view='bibtex')",
                        "BibTeX citation",
                    ),
                ]
            )
        )

    return "\n".join(lines)


def _count_sections(s: Section) -> int:
    return 1 + sum(_count_sections(c) for c in s.children)


def _collect_rows(
    s: Section,
    *,
    depth: int,
    blocks_by_pos: dict[int, Block] | None,
) -> list[tuple[int, str, str, str]]:
    """Flatten the section tree into row tuples for column alignment."""
    rng = f"~{s.start}..{s.end}"
    count = f"({s.block_count})"
    label = _section_label(s, blocks_by_pos=blocks_by_pos)
    rows: list[tuple[int, str, str, str]] = [(depth, rng, count, label)]
    for child in s.children:
        rows.extend(_collect_rows(child, depth=depth + 1, blocks_by_pos=blocks_by_pos))
    return rows


def _section_label(
    s: Section,
    *,
    blocks_by_pos: dict[int, Block] | None,
) -> str:
    """Render the title column for a section row.

    For implicit (untitled) leading sections, derive a one-line preview
    from the first body block so the row isn't blank.
    """
    if s.title:
        prefix = "■ " if s.level == 1 else ""
        return f"{prefix}{s.title}"
    # Implicit / untitled — preview the first body block if we have it.
    if blocks_by_pos:
        b = blocks_by_pos.get(s.start)
        if b is not None:
            preview = " ".join(b.text.split())[:80]
            return f"<untitled>  {preview}"
    return "<untitled>"
