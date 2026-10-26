"""Deterministic parse of a Marker pipe-table chunk into a grid.

Pure, no I/O: :func:`parse_table` turns the verbatim text of a
``chunk_kind='table'`` chunk into columns, data rows and cells with
exact char offsets back into the chunk — the anchor the recipe slice
(``extract/recipes.py``) writes through ``insert_measure``. A chunk
that is not a well-formed pipe grid (collapsed OCR runs, HTML tables)
returns ``None``; the caller renders raw text and never guesses
columns.

The one definition of a pipe-table line and separator lives here:
``extract/proposals.py`` imports ``_table_line_cells`` /
``_is_table_separator`` back from this module rather than keeping its
own copy.

The citation/footnote/unit helpers are *detection*, never trust: a
header or row label carrying a citation marker (``et al.``,
``<sup>56</sup>``, ``[12]``, ``ref.``) flags the column or row as
``cited_work`` whatever a later recipe claims (decision 6 of
``docs/backlog/table-numeric-extraction.md``).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from precis.errors import BadInput
from precis.taxonomy.measure_units import _parse_unit, parse_literal
from precis.utils.text import markup_unit


@dataclass(frozen=True, slots=True)
class Cell:
    """One grid cell, verbatim-anchored into the chunk text."""

    text: str  # exact cell text, verbatim slice of the chunk
    start: int  # offset of text in chunk.text
    end: int
    value_text: str  # text with a trailing footnote marker removed
    footnote: str | None  # 'a', 'b', '*', '†' … or None
    numeric: bool  # parse_literal(value_text).form not in ('categorical',)


@dataclass(frozen=True, slots=True)
class Column:
    """One column of the parsed grid."""

    index: int
    header: str  # header cell(s) joined with ' / ' for a 2-row header
    header_unit: str | None  # deterministic unit guess from the header
    cited: bool  # header carries a citation marker


@dataclass(frozen=True, slots=True)
class ParsedTable:
    """The parsed grid of one ``chunk_kind='table'`` chunk."""

    chunk_id: int
    columns: list[Column]
    rows: list[list[Cell]]  # data rows only, separator dropped
    row_spans: list[tuple[int, int]]  # offsets of each data row line
    header_rows: int  # 0, 1 or 2
    header_lines: list[str]  # the raw header lines — a continuation parses against them
    ragged_rows: list[int]  # data-row indices whose cell count != header's
    row_label_cited: list[bool]  # per row: label cell carries a citation marker


def _table_line_cells(text: str) -> list[tuple[int, int, int]]:
    """``(start, end, cell_count)`` for every line that reads as a
    pipe-table row — at least one ``|`` separating cells. An empty cell
    still counts: it keeps its column ('| a | | b |' is three cells)."""
    rows = []
    pos = 0
    # ``split("\n")``, never ``splitlines()``: a ``\r\n`` chunk keeps
    # its ``\r`` inside the line, so ``pos = end + 1`` stays an exact
    # one-char advance and the ``(start, end)`` anchors stay true
    # (``splitlines`` also breaks on ``\r``/``\x85``/``\u2028`` — a
    # two-char break would drift every later span).
    for line in text.split("\n"):
        start, end = pos, pos + len(line)
        pos = end + 1
        if "|" not in line:
            continue
        rows.append((start, end, len(line.strip().strip("|").split("|"))))
    return rows


def _is_table_separator(line: str) -> bool:
    """A ``|---|`` divider row: every cell is only dashes/colons.

    ``ingest/marker.py::_is_table_separator`` semantics — at least two
    dashes per cell, optional colons — so a ``| - | - |`` data row is a
    data row, not a divider."""
    cells = [c.strip() for c in line.strip().strip("|").split("|")]
    return bool(cells) and all(re.fullmatch(r":?-{2,}:?", c) for c in cells)


def _row_cells(line: str, line_start: int) -> list[tuple[str, int, int]]:
    """``(stripped_text, start, end)`` per cell of a ``|``-line, with
    ``text[start:end] == stripped_text`` exact (empty cells keep a
    zero-width span at their position)."""
    parts = line.split("|")
    first = 1 if line.lstrip().startswith("|") else 0
    last = len(parts) - 1 if line.rstrip().endswith("|") else len(parts)
    cells: list[tuple[str, int, int]] = []
    pos = line_start
    for i, part in enumerate(parts):
        part_start = pos
        pos += len(part) + 1  # + the pipe
        if i < first or i >= last:
            continue
        stripped = part.strip()
        lead = len(part) - len(part.lstrip())
        start = part_start + lead
        cells.append((stripped, start, start + len(stripped)))
    return cells


#: A single letter or symbol footnote marker — ``^a``, ``^{b}``,
#: ``<sup>a</sup>``, ``*``, ``†``, ``‡``, ``§``. ``^{-2}`` and
#: ``<sup>-1</sup>`` are exponents and never reach this set.
_FOOTNOTE_MARK = r"[A-Za-z*†‡§]"
_FOOTNOTE_RE = re.compile(
    rf"(?P<body>.*?)(?:\^\{{(?P<mk>{_FOOTNOTE_MARK})\}}"
    rf"|\^(?P<mk2>{_FOOTNOTE_MARK})"
    rf"|<sup>(?P<mk3>{_FOOTNOTE_MARK})</sup>"
    rf"|(?P<mk4>[*†‡§]))(?P<tail>\s*\$?\s*)$"
)


def strip_footnote(cell: str) -> tuple[str, str | None]:
    """Split a trailing footnote marker off a cell's value text.

    ``13.5 \\pm 1.1^a`` → ``('13.5 \\pm 1.1', 'a')``; the marker may sit
    inside a closing ``$`` (``$18.5 \\pm 2.0^{b}$``). Only a single
    letter or symbol counts and only after a number (the marker-free
    text must end in a digit, ``}`` or ``%``), so ``^{-2}`` /
    ``<sup>-1</sup>`` exponents and ``\\mu^*`` symbol cells stay whole.
    """
    m = _FOOTNOTE_RE.match(cell.strip())
    if m is None:
        return cell.strip(), None
    body = m.group("body")
    if not re.search(r"[\d}%]\s*\$?\s*$", body):
        return cell.strip(), None
    mark = next(g for g in m.groups()[1:5] if g is not None)
    value = body + ("$" if m.group("tail").strip() else "")
    return value.strip(), mark


_CITATION_RES = (
    re.compile(r"et\s+al\b", re.IGNORECASE),
    re.compile(r"<sup>\s*\d+(?:\s*,\s*\d+)*\s*</sup>"),
    # A caret-braced number is a citation only when it isn't a positive
    # exponent — ``10^{12}`` must not flag the column.
    re.compile(r"(?<![\d{])\^\{?\d+(?:\s*,\s*\d+)*\}?"),
    re.compile(r"\[\d+(?:\s*,\s*\d+)*\]"),
    re.compile(r"\brefs?\.", re.IGNORECASE),
)


def has_citation_marker(cell: str) -> bool:
    """True when the cell cites another work: ``et al``, ``<sup>56</sup>``,
    ``^{56}``, ``[12]``/``[12,13]`` or ``ref.``/``refs.``
    (case-insensitive)."""
    return any(rx.search(cell) for rx in _CITATION_RES)


#: ``( … )`` / ``[ … ]`` groups — the last one on a header is the unit
#: candidate ('rate constant K_i N_0 (L^{-2})', 'coverage (ML)',
#: 'prefactor [kJ/mol]').
_UNIT_GROUP_RE = re.compile(r"\(([^()]*)\)|\[([^\][]*)\]")

#: Dimensionless labels kept as hints even though pint reads ``ML`` as
#: megalitre and ``%`` needs its percent remap.
_DIMENSIONLESS_HINTS = frozenset({"ML", "%", "1"})


def header_unit(header: str) -> str | None:
    """The unit a column header names in its last parenthesised or
    bracketed group, markup-folded (``(s <sup>-1</sup> )`` → ``s^-1``,
    ``[kJ/mol]`` → ``kJ/mol``). Kept only when pint parses it as a unit
    or it is one of the dimensionless labels ``ML``/``%``/``1`` — a
    display hint for the recipe and the renderer, never a stored unit
    on its own."""
    matches = list(_UNIT_GROUP_RE.finditer(header))
    if not matches:
        return None
    m = matches[-1]
    raw = m.group(1) if m.group(1) is not None else (m.group(2) or "")
    candidate = markup_unit(raw).strip()
    # ``s <sup>-1</sup>`` folds to ``s ^-1`` — the space before a caret
    # exponent is markup spill, not a second unit token.
    candidate = re.sub(r"\s+\^(?=-?\d)", "^", candidate)
    if not candidate:
        return None
    if candidate in _DIMENSIONLESS_HINTS:
        return candidate
    try:
        _parse_unit(candidate, what="header")
    except BadInput:
        return None
    return candidate


#: A caption line opens ``Table <n>`` followed by a delimiter — ``.``
#: or ``:`` (``Table 2. Kinetic parameters``, ``Table 2: rates``) or a
#: closing ``**`` (``**Table 2** rates``). A bare number with prose
#: after it (``Table 2 shows the rates``) is an in-text reference, not
#: a caption — the delimiter requirement is what keeps it out.
_CAPTION_RE = re.compile(
    r"^\s*(?:\*\*)?(?:Table|TABLE)\s+(?P<num>[0-9IVXS]+)(?:[.:]|\*\*)"
)


def _caption_number(token: str) -> int | None:
    """The ordinal a caption token carries: ``'2'`` → 2, ``'IV'`` → 4.
    ``'S1'`` and other supplementary labels return ``None`` — a label,
    not an ordinal, so it can't constrain which table the caption
    belongs to."""
    if token.isdigit():
        return int(token)
    vals = {"I": 1, "V": 5, "X": 10}
    total, prev = 0, 0
    for ch in reversed(token):
        v = vals.get(ch)
        if v is None:
            return None
        total += -v if v < prev else v
        prev = max(prev, v)
    return total or None


def first_pipe_line(text: str) -> str | None:
    """The chunk's table header — its first ``|`` line, returned only
    when the *next* ``|`` line is a ``|---|`` separator, i.e. the
    chunk opens on a real header row. ``None`` when the chunk holds
    no pipe grid at all (a ``hard_max``/``split_text`` fallback
    piece) or opens mid-table on a data row — ``enforce_hard_max``
    cuts an oversized part mid-body, and its continuation pieces
    must join their run (``table_runs`` admits ``None``) rather than
    split off as a new logical table that can steal the next table's
    numbered caption. ``split_table`` prepends the original header —
    row plus separator — to every part it writes, so equal first
    pipe lines are the evidence that two adjacent table chunks are
    parts of one table rather than two tables printed back to
    back."""
    pipe = _pipe_lines(text)
    if len(pipe) >= 2 and _is_table_separator(pipe[1]):
        return pipe[0]
    return None


def _pipe_lines(text: str) -> list[str]:
    return [ln.strip() for ln in text.split("\n") if ln.lstrip().startswith("|")]


@dataclass(frozen=True, slots=True)
class TableSig:
    """Run evidence for one ``chunk_kind='table'`` chunk — the fields
    :func:`table_runs` groups on (gr506762)."""

    header: str | None  # separator-backed first pipe line, else None
    ncols: int  # cell count of the first pipe line; 0 = no pipe grid
    caption: bool  # the chunk opens on a "Table N" caption line


def table_sig(text: str) -> TableSig:
    """The evidence ``table_runs`` groups on: the separator-backed
    header (``None`` for a no-grid or mid-table piece), the first
    pipe line's cell count, and whether the chunk opens on a
    ``Table N`` caption line — a caption-start chunk is always a new
    logical table, and a header-less piece joins its run only while
    it plausibly continues it (same column count). The column count
    comes from ``_row_cells``, the splitter ``parse_table`` uses —
    ``"| x ||"`` (a trailing empty cell) counts as two columns, not
    the one a ``strip('|')`` first loses."""
    pipe = _pipe_lines(text)
    header = pipe[0] if len(pipe) >= 2 and _is_table_separator(pipe[1]) else None
    ncols = len(_row_cells(pipe[0], 0)) if pipe else 0
    return TableSig(header, ncols, _CAPTION_RE.match(text) is not None)


def table_runs(
    tables: Sequence[tuple[int, int, TableSig]],
) -> list[list[tuple[int, int]]]:
    """Group table chunks into logical tables.

    Ingest splits a large pipe table into several consecutive
    ``chunk_kind='table'`` chunks (``ingest/marker.py::split_table`` /
    ``enforce_hard_max``), so a chunk's position among the ref's table
    chunks is NOT the paper's table number. ``tables`` is
    ``(chunk_id, ord, table_sig)`` in ord order — see
    :func:`table_sig`. A chunk joins the current run only when its
    ord is adjacent AND its evidence says it continues the same
    table — a separator-backed header equal to the run's (the header
    ``split_table`` prepends to every part), or no header evidence
    (a no-grid fallback piece, or an ``enforce_hard_max`` continuation
    opening mid-table on a data row) with a first-pipe-line cell
    count equal to the run's. The run's evidence is its LAST member
    holding a grid (``ncols > 0``), not its head: a no-grid head
    would otherwise let any later header-less chunk join, fusing two
    different separator-less tables behind it. Two break rules hold
    either way: a chunk opening on a ``Table N`` caption line always
    starts a new run, and a header-less piece whose column count
    differs from the run's is a different table — two separator-less
    tables printed back to back must not fuse (Opus ruling, gr506762).

    Returns the groups in ord order as ``(chunk_id, ord)`` runs; a
    lone chunk is a group of one."""
    runs: list[list[tuple[int, int]]] = []
    sigs: list[TableSig] = []  # per run: the last member's sig holding a grid
    for t_id, t_ord, sig in tables:
        base = sigs[-1] if sigs else None
        if (
            runs
            and t_ord == runs[-1][-1][1] + 1
            and not sig.caption
            and base is not None
            and (
                (sig.header is not None and sig.header == base.header)
                or (
                    sig.header is None
                    and (sig.ncols == 0 or base.ncols == 0 or sig.ncols == base.ncols)
                )
            )
        ):
            runs[-1].append((t_id, t_ord))
            if sig.ncols > 0:
                # The base keeps the run's header evidence: a hard_max
                # continuation carries ``header=None``, and blanking it
                # would break the next ``split_table`` part's join.
                sigs[-1] = TableSig(sig.header or base.header, sig.ncols, base.caption)
        else:
            runs.append([(t_id, t_ord)])
            sigs.append(sig)
    return runs


def assign_captions(
    tables: Sequence[tuple[int, int, TableSig]],
    neighbours: Sequence[tuple[int, int, str]],
    window: int = 2,
) -> dict[int, tuple[int, str]]:
    """Pair each logical table with at most one caption chunk, one-to-one.

    ``tables`` is ``(chunk_id, ord, table_sig)`` for the ref's
    table chunks in ord order. Ord-adjacent same-evidence runs collapse
    into logical tables via :func:`table_runs` — a table's number is
    its group index, and a
    caption whose number parses (``Table 2``, ``Table IV``) may only
    attach to the group with that number: a missing ``Table 2`` caption
    can never be borrowed from ``Table 1``'s neighbour, and a caption
    shared between two groups in one window is claimed by the nearer
    (numbered matches rank ahead of unnumbered ones at the same
    distance, and a caption before the group beats one after on a true
    tie). The ``±window`` is measured from the group's first/last ord,
    and the caption attaches to *every* chunk in the group — sharing
    within one split table is correct. A group left unpaired gets no
    caption — an ambiguous guess is worse than none (the recipe slice
    would anchor wrong conditions under it).

    ``neighbours`` is ``(chunk_id, ord, text)`` — the non-table chunks
    around the tables. Returns ``{table_chunk_id: (caption_chunk_id,
    first 300 chars)}``.
    """
    cands: list[tuple[int, int, int | None, str]] = []
    for chunk_id, ord_, text in neighbours:
        m = _CAPTION_RE.match(text or "")
        if m is not None:
            cands.append((chunk_id, ord_, _caption_number(m.group("num")), text))
    runs = table_runs(tables)
    # Every eligible (distance, ranked?, before?, …) group/caption
    # pair; greedy assignment in distance order claims each caption
    # once and each group once.
    pairs: list[tuple[int, int, int, int, int, str]] = []
    for g_idx, run in enumerate(runs):
        lo, hi = run[0][1], run[-1][1]
        for c_id, c_ord, num, c_text in cands:
            dist = lo - c_ord if c_ord < lo else c_ord - hi
            if dist <= 0 or dist > window:
                continue
            if num is not None and num != g_idx + 1:
                continue
            pairs.append(
                (
                    dist,
                    0 if num is not None else 1,
                    0 if c_ord < lo else 1,
                    g_idx,
                    c_id,
                    c_text,
                )
            )
    pairs.sort(key=lambda p: p[:3])
    per_group: dict[int, tuple[int, str]] = {}
    used: set[int] = set()
    for _d, _r, _b, g_idx, c_id, c_text in pairs:
        if g_idx in per_group or c_id in used:
            continue
        per_group[g_idx] = (c_id, c_text[:300])
        used.add(c_id)
    return {
        t_id: cap
        for g_idx, run in enumerate(runs)
        if (cap := per_group.get(g_idx)) is not None
        for t_id, _o in run
    }


def _numeric_cell(text: str) -> bool:
    return parse_literal(strip_footnote(text)[0]).form != "categorical"


def parse_table(
    chunk_id: int, text: str, *, header: Sequence[str] | None = None
) -> ParsedTable | None:
    """Parse a ``chunk_kind='table'`` chunk's pipe grid, or ``None``
    when fewer than two lines start with ``|`` (one data row is
    enough when ``header`` supplies the header externally).

    ``header`` is the run head's header lines for an
    ``enforce_hard_max`` continuation: its own text opens mid-table
    on a data row, so the run's header is supplied rather than letting
    row 0 be eaten as a header (gr506762 — the recipe path's column
    names, cited flags and 1-based row numbers all depend on it).
    Every cell's ``text`` is still a verbatim slice of THIS chunk —
    ``text[cell.start:cell.end] == cell.text`` always holds (the
    recipe slice's anchors depend on it); the supplied header lines
    only build the ``Column`` records and never anchor anything. With
    no ``header``, the separator line marks the end of the header;
    with no separator, row 0 is the header and row 1 joins it when
    row 1 has no numeric cell while row 0 has empty cells (the
    transposed 'sources in columns' shape).
    """
    pos = 0
    pipe_rows: list[tuple[int, int, str]] = []
    # ``split("\n")`` for the same reason as ``_table_line_cells``: a
    # ``\r\n`` break keeps the ``\r`` on the line (stripped by
    # ``_row_cells`` when it lands inside a cell) and ``pos = end + 1``
    # remains an exact advance, so ``text[cell.start:cell.end]`` stays
    # a verbatim slice on CRLF input.
    for line in text.split("\n"):
        start, end = pos, pos + len(line)
        pos = end + 1
        if line.lstrip().startswith("|"):
            pipe_rows.append((start, end, line))
    if len(pipe_rows) < (1 if header else 2):
        return None

    if header:
        # The header lives in the run head's text — its cell offsets
        # index that line, not this chunk, and are only ever used for
        # ``Column`` records.
        raw_header = list(header)
        header_lines = [_row_cells(ln, 0) for ln in raw_header]
        header_rows = len(header_lines)
        data_rows = pipe_rows
    else:
        sep_idx = next(
            (i for i, (_s, _e, ln) in enumerate(pipe_rows) if _is_table_separator(ln)),
            None,
        )
        if sep_idx is not None:
            header_rows = min(sep_idx, 2)
        else:
            header_rows = 1
            r0 = _row_cells(pipe_rows[0][2], pipe_rows[0][0])
            r1 = _row_cells(pipe_rows[1][2], pipe_rows[1][0])
            if any(not cell for cell, _s, _e in r0) and not any(
                _numeric_cell(cell) for cell, _s, _e in r1
            ):
                header_rows = 2

        raw_header = [ln for _s, _e, ln in pipe_rows[:header_rows]]
        header_lines = [
            _row_cells(pipe_rows[i][2], pipe_rows[i][0]) for i in range(header_rows)
        ]
        data_rows = pipe_rows[header_rows:]
    ncols = len(header_lines[0]) if header_lines else 0

    columns: list[Column] = []
    for i in range(ncols):
        parts = [hl[i][0] for hl in header_lines if i < len(hl) and hl[i][0].strip()]
        header = " / ".join(parts)
        columns.append(
            Column(
                index=i,
                header=header,
                header_unit=header_unit(header) if header else None,
                cited=has_citation_marker(header) if header else False,
            )
        )

    rows: list[list[Cell]] = []
    row_spans: list[tuple[int, int]] = []
    ragged_rows: list[int] = []
    row_label_cited: list[bool] = []
    for start, end, line in data_rows:
        if _is_table_separator(line):
            continue
        cells: list[Cell] = []
        for cell_text, cs, ce in _row_cells(line, start):
            value_text, footnote = strip_footnote(cell_text)
            cells.append(
                Cell(
                    text=cell_text,
                    start=cs,
                    end=ce,
                    value_text=value_text,
                    footnote=footnote,
                    numeric=parse_literal(value_text).form != "categorical",
                )
            )
        row_idx = len(rows)
        if len(cells) != ncols:
            ragged_rows.append(row_idx)
        row_label_cited.append(bool(cells) and has_citation_marker(cells[0].text))
        rows.append(cells)
        row_spans.append((start, end))

    return ParsedTable(
        chunk_id=chunk_id,
        columns=columns,
        rows=rows,
        row_spans=row_spans,
        header_rows=header_rows,
        header_lines=raw_header,
        ragged_rows=ragged_rows,
        row_label_cited=row_label_cited,
    )


def run_headers(
    texts: Mapping[int, str],
    tables: Sequence[tuple[int, int, TableSig]],
    runs: Sequence[Sequence[tuple[int, int]]],
) -> dict[int, list[str]]:
    """``chunk_id -> run head's raw header lines`` for the table chunks
    whose own text lacks a separator-backed header — the
    ``enforce_hard_max`` continuations that must parse against the
    head's header (``parse_table(header=…)``) so a first data row
    isn't eaten as one (gr506762). ``texts`` maps chunk_id → chunk
    text; ``tables``/``runs`` are the ``table_sig`` triples and the
    :func:`table_runs` groups. Shared by ``survey_extract`` and the
    ``view='tables'`` renderer so stored ``meta.table`` row numbers
    and the view's grid agree."""
    sig_of = {cid: sig for cid, _o, sig in tables}
    out: dict[int, list[str]] = {}
    for run in runs:
        if len(run) < 2:
            continue
        # The header source is the first member that HAS one, not
        # blindly chunk 1 — a gridless head piece carries none.
        head = next((cid for cid, _o in run if sig_of[cid].header is not None), None)
        if head is None:
            continue
        parsed = parse_table(head, texts[head])
        if parsed is None:
            continue
        for cid, _o in run:
            if sig_of[cid].header is None:
                out[cid] = parsed.header_lines
    return out
