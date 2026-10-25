"""``get(kind='paper', view='tables')`` — the parsed-grid read of a
paper's ``chunk_kind='table'`` chunks (gr506762 slice 1).

One store read (``ChunkStore.table_chunks_for_ref``) yields the table
chunks and their ±2-ord neighbours for the caption lookup; each table
parses through :func:`precis.extract.tables.parse_table` — a chunk that
isn't a well-formed pipe grid renders raw, never guessed at.

Every logical table carries a ``T<n>`` ordinal — its 1-based position
among the paper's *table runs* (:func:`precis.extract.tables.table_runs`;
ingest splits a large table into ord-adjacent table chunks, which stay
one ``T<n>`` here, continuations labelled ``T1 (part 2)``) — its ``pc``
handle, a caption line when a neighbouring ``Table <n>`` chunk is found,
the header-unit hints, and a ``measures: N extracted`` counter (rows
anchored on that chunk). Cells cite as ``pc<id> row R col C`` — the
cell anchor the recipe slice writes through ``insert_measure``.

``args={'table': n|<pc-handle>}`` renders one table;
``args={'match': 'sticking prefactor'}`` keeps tables whose header,
caption or row labels hold any of the words (case-insensitive, on the
``markup_surface`` text).

Cell cites are 1-based on both axes: ``pc<id> row R col C`` names
grid row ``R`` (the ``| R`` gutter) and column ``C`` — ``col 1`` is
the row-label column, matching the ``[n]`` markers in the
``columns:`` line and the grid header.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from precis.errors import BadInput
from precis.extract.tables import (
    ParsedTable,
    assign_captions,
    first_pipe_line,
    parse_table,
    table_runs,
)
from precis.response import Response
from precis.utils import handle_registry
from precis.utils.next_block import render_next_section
from precis.utils.text import markup_surface

if TYPE_CHECKING:
    from precis.store import Ref, Store

#: Logical tables (groups) fully rendered before the fold — never
#: slices a table in half; the hidden ones get a one-line pointer each.
_TABLE_BUDGET = 12

#: Data rows shown per table before a fold line.
_ROW_CAP = 50

#: Parts of one split table rendered before a per-run fold line —
#: an explicit ``args={'table': n}`` shows the whole run.
_PART_CAP = 3


def _pa(ref: Ref) -> str:
    return (
        handle_registry.try_format(ref.kind, ref.id)
        or ref.slug
        or f"{ref.kind}:{ref.id}"
    )


def _chunk_handle(kind: str, chunk_id: int) -> str:
    return handle_registry.try_format(kind, chunk_id, chunk=True) or f"chunk:{chunk_id}"


def _match_hit(
    words: list[str],
    parsed_headers: list[str],
    caption: str | None,
    row_labels: list[str],
) -> bool:
    hay = markup_surface(
        " ".join(parsed_headers + row_labels + [caption or ""])
    ).lower()
    return any(w in hay for w in words)


def _select_table(sel: Any, runs: list[list[tuple[int, int]]], store: Store) -> int:
    """``args['table']`` → the 0-based index into ``runs`` (logical
    tables — a split table's chunks select as one).

    Accepts the 1-based ``T<n>`` ordinal or a ``pc`` chunk handle. An
    out-of-range ordinal or a handle naming a non-table chunk is a
    BadInput naming the valid values."""
    raw = str(sel).strip()
    n_tables = len(runs)
    if raw.isdigit():
        n = int(raw)
        if 1 <= n <= n_tables:
            return n - 1
        raise BadInput(
            f"args={{'table': {n}}} is out of range — the paper has "
            f"{n_tables} table{'s' if n_tables != 1 else ''} (T1..T{n_tables})",
            next="args={'table': 1} renders the first",
        )
    parsed = handle_registry.parse(raw)
    resolved = store.resolve_handle(raw) if parsed is not None else None
    if resolved is not None and resolved.chunk_id is not None:
        for g_idx, run in enumerate(runs):
            if any(cid == resolved.chunk_id for cid, _o in run):
                return g_idx
        raise BadInput(
            f"{raw} is a chunk of this paper but not a table chunk",
            options=[f"T{i + 1}" for i in range(n_tables)],
            next="args={'table': N} takes the 1-based ordinal, or a pc table handle",
        )
    raise BadInput(
        f"args={{'table': {raw!r}}} wants a 1-based ordinal or a pc chunk handle",
        next="args={'table': 1} renders the first table",
    )


def _render_table(
    *,
    kind: str,
    label: str,
    chunk_id: int,
    text: str,
    section_path: list[str],
    caption: tuple[int, str] | None,
    measure_count: int,
    parsed: ParsedTable | None,
) -> str:
    handle = _chunk_handle(kind, chunk_id)
    section = f" · §{section_path[-1]}" if section_path else ""
    lines = [f"## {label} · {handle}{section}"]
    if caption is not None:
        cap_id, cap_text = caption
        lines.append(
            f"caption ({_chunk_handle(kind, cap_id)}): {markup_surface(cap_text)}"
        )
    if parsed is None:
        lines.append("unparsed — raw text:")
        lines.append(markup_surface(text)[:600])
        return "\n".join(lines)

    col_parts: list[str] = []
    for col in parsed.columns:
        if not col.header.strip():
            continue
        piece = f"[{col.index + 1}] {markup_surface(col.header)}"
        if col.header_unit:
            piece += f" [unit {col.header_unit}]"
        if col.cited:
            piece += " (cited)"
        col_parts.append(piece)
    if col_parts:
        lines.append("columns: " + " · ".join(col_parts))

    ncols = len(parsed.columns)
    # Columns cite 1-based: ``col 1`` is the row-label column, so the
    # grid header numbers the data columns 2..ncols.
    head = ["| row", "label"] + [f"[{i}]" for i in range(2, ncols + 1)] + ["|"]
    lines.append(" ".join(head))
    for r_idx, (row, ragged) in enumerate(
        zip(parsed.rows, _ragged_set(parsed.ragged_rows, len(parsed.rows)))
    ):
        if r_idx >= _ROW_CAP:
            lines.append(f"| … {len(parsed.rows) - _ROW_CAP} more rows |")
            break
        cells = [
            markup_surface(c.value_text) + (f"^{c.footnote}" if c.footnote else "")
            for c in row
        ]
        while len(cells) < ncols:
            cells.append("")
        line = f"| {r_idx + 1} | " + " | ".join(cells[:ncols]) + " |"
        if ragged:
            line += " ⚠ ragged"
        lines.append(line)
    lines.append(
        f"measures: {measure_count} extracted · cite a cell as {handle} row R col C"
    )
    return "\n".join(lines)


def _ragged_set(ragged: list[int], n_rows: int) -> list[bool]:
    flags = [False] * n_rows
    for i in ragged:
        if 0 <= i < n_rows:
            flags[i] = True
    return flags


def render_tables_view(
    store: Store,
    ref: Ref,
    *,
    args: dict[str, Any] | None,
) -> Response:
    """``view='tables'`` — every ``chunk_kind='table'`` chunk of the
    paper as a parsed grid, in ord order."""
    tables, neighbours = store.chunks.table_chunks_for_ref(ref.id)
    pa = _pa(ref)
    if not tables:
        body = f"no tables in {pa}"
        body += render_next_section(
            [
                (
                    f"get(id='{pa}', view='chunks')",
                    "chunk counts by kind — the paper may hold no body or no tables",
                ),
            ]
        )
        return Response(body=body)

    sel = args.get("table") if isinstance(args, dict) else None
    match = args.get("match") if isinstance(args, dict) else None
    words = [w for w in str(match or "").lower().split() if w]

    # Logical tables = runs of ord-adjacent same-header table chunks
    # (ingest splits big tables). ``T<n>`` and ``args={'table': n}``
    # number the runs; a match hit on any part shows the whole run.
    triples = [(cid, ord_, first_pipe_line(t)) for cid, ord_, t, _sp in tables]
    runs = table_runs(triples)
    idx_of = {t[0]: i for i, t in enumerate(tables)}
    part_of = {cid: p for run in runs for p, (cid, _o) in enumerate(run, 1)}
    show: list[int] = list(range(len(runs)))
    if sel is not None:
        show = [_select_table(sel, runs, store)]

    # One-to-one caption pairing across the whole ref (see
    # ``assign_captions`` — a numbered caption only ever lands on its
    # own numbered table, and one caption never serves two tables).
    captions = assign_captions(triples, neighbours)
    measure_counts = store.measure_counts_by_anchor_chunk([t[0] for t in tables])

    sections: list[str] = []
    folded: list[int] = []
    shown_groups = 0
    for g_idx in show:
        run = runs[g_idx]
        # Parse once per part, and only when the match filter needs it
        # or the run will actually render — folded past-budget runs
        # stay unparsed. ``match`` is group-level: a hit on any part
        # keeps every part.
        parsed_by_i: dict[int, ParsedTable | None] = {}
        if words:
            hit = False
            for cid, _o in run:
                i = idx_of[cid]
                _c, _o2, text, _sp = tables[i]
                parsed = parse_table(cid, text)
                parsed_by_i[i] = parsed
                headers = (
                    [c.header for c in parsed.columns] if parsed is not None else []
                )
                labels = [r[0].text for r in parsed.rows] if parsed is not None else []
                cap = captions.get(cid)
                if _match_hit(
                    words,
                    headers or [text[:200]],
                    cap[1] if cap is not None else None,
                    labels,
                ):
                    hit = True
            if not hit:
                continue
        if shown_groups >= _TABLE_BUDGET:
            folded.append(g_idx)
            continue
        shown_groups += 1
        # Parts beyond _PART_CAP fold with a count — an explicit
        # ``args={'table': n}`` selection renders the whole run.
        parts = run if sel is not None else run[:_PART_CAP]
        for cid, _o in parts:
            i = idx_of[cid]
            _c, _o2, text, section_path = tables[i]
            if i not in parsed_by_i:
                parsed_by_i[i] = parse_table(cid, text)
            part = part_of[cid]
            label = f"T{g_idx + 1}" if part == 1 else f"T{g_idx + 1} (part {part})"
            sections.append(
                _render_table(
                    kind=ref.kind,
                    label=label,
                    chunk_id=cid,
                    text=text,
                    section_path=section_path,
                    caption=captions.get(cid),
                    measure_count=measure_counts.get(cid, 0),
                    parsed=parsed_by_i[i],
                )
            )
        hidden = len(run) - len(parts)
        if hidden:
            sections.append(
                f"… {hidden} more part{'s' if hidden != 1 else ''} of "
                f"T{g_idx + 1} — args={{'table': {g_idx + 1}}}"
            )
    for g_idx in folded:
        handle = _chunk_handle(ref.kind, runs[g_idx][0][0])
        sections.append(f"… T{g_idx + 1} {handle} — args={{'table': {g_idx + 1}}}")

    if not sections:
        what = (
            f"match={str(match).strip()!r}" if match is not None else f"table={sel!r}"
        )
        body = f"no tables in {pa} match {what}"
        body += render_next_section(
            [
                (
                    f"get(id='{pa}', view='tables')",
                    "list every table unfiltered",
                ),
            ]
        )
        return Response(body=body)

    n = len(runs)
    head = f"# {pa} — {n} table{'s' if n != 1 else ''}"
    if sel is not None or match is not None:
        head += f" (showing {shown_groups} table{'s' if shown_groups != 1 else ''})"
    body = head + "\n\n" + "\n\n".join(sections)
    body += render_next_section(
        [
            (
                "search(kind='paper', q='…', args={'chunk_kind': 'table'})",
                "table-chunk hits across the corpus",
            ),
            (
                f"get(id='{pa}', view='chunks')",
                "chunk counts by kind",
            ),
        ]
    )
    return Response(body=body, result_count=shown_groups)
