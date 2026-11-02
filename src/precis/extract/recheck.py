"""Re-check stored ``table_recipe`` measure rows against the current
extraction guards, and locate a table row's review context at read
time (gr506762 subset-4).

Two consumers, one engine:

* ``measure_accept`` — rows written before ``meta.table`` carried the
  header spans and labels got a bare data row for a span and read
  'undecidable'. :func:`review_table_ctx` re-derives the column header
  row, caption and units from the row's anchor chunk at review time,
  so every table-derived row is judged on the same evidence without a
  data rewrite; :func:`precis.extract.recipes.anchor_cell_mismatch`
  then checks the literal sits at the claimed row-label/column-header
  intersection.
* ``put(kind='measure', mode='recheck')`` —
  :func:`measure_recheck` re-runs the deterministic refusals
  (:func:`precis.extract.recipes.stored_row_reasons`) over the live
  ``table_recipe`` rows of the given papers and lists which would now
  be refused. ``dry_run`` defaults to True and only lists; a
  non-dry-run apply records a ``rejected`` ledger review per refused
  row — the retraction the measure kind already uses (a rejected
  newest current review drops a row out of every ranking). It is a
  hand-run command, never a cadence.

The per-paper table lookups (``table_runs`` grouping, continuation
headers, caption pairing) mirror ``survey_extract``'s extract-time
computation exactly, so a chunk parses the same way at review and
recheck as it did when the recipe ran.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from precis.errors import BadInput
from precis.extract import recipes as _recipes
from precis.extract.proposals import vocab_entry
from precis.extract.tables import (
    assign_captions,
    header_unit,
    parse_table,
    run_headers,
    table_runs,
    table_sig,
)
from precis.utils import handle_registry

if TYPE_CHECKING:
    from precis.extract.tables import ParsedTable
    from precis.store.store import Store


def _paper_chunks(store: Store, paper: int) -> list[dict[str, Any]]:
    """The paper's live body chunks, ord order — the same SELECT as
    ``survey_extract``'s ``_chunks``."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT chunk_id, ord, chunk_kind, text FROM chunks "
            "WHERE ref_id = %s AND ord >= 0 AND retired_at IS NULL ORDER BY ord",
            (paper,),
        ).fetchall()
    return [
        {
            "chunk_id": int(cid),
            "ord": int(ord_),
            "chunk_kind": str(kind),
            "text": str(text or ""),
        }
        for cid, ord_, kind, text in rows
    ]


def table_index(chunks: list[dict[str, Any]]) -> dict[str, Any]:
    """One paper's table lookups: logical-table numbers, continuation
    headers and caption pairings — the same ``table_runs`` /
    ``run_headers`` / ``assign_captions`` computation the extract pass
    runs (gr506762 subset-4)."""
    table_seq = [
        (c["chunk_id"], c["ord"], table_sig(c["text"]))
        for c in chunks
        if c["chunk_kind"] == "table"
    ]
    captions = assign_captions(
        table_seq,
        [
            (c["chunk_id"], c["ord"], c["text"])
            for c in chunks
            if c["chunk_kind"] != "table"
        ],
    )
    runs = table_runs(table_seq)
    return {
        "texts": {c["chunk_id"]: c["text"] for c in chunks},
        "kinds": {c["chunk_id"]: c["chunk_kind"] for c in chunks},
        "captions": captions,
        "table_no": {cid: g + 1 for g, run in enumerate(runs) for cid, _o in run},
        "headers": run_headers(
            {c["chunk_id"]: c["text"] for c in chunks}, table_seq, runs
        ),
    }


def parsed_for(idx: dict[str, Any], chunk_id: Any) -> ParsedTable | None:
    """``parse_table`` for ``chunk_id`` under the paper's table index —
    a continuation parses against the run head's header lines, exactly
    as the extract pass parsed it."""
    text = idx["texts"].get(chunk_id)
    if text is None or idx["kinds"].get(chunk_id) != "table":
        return None
    return parse_table(chunk_id, text, header=idx["headers"].get(chunk_id))


def locate_cell(
    parsed: ParsedTable,
    span: Any,
    literal: str = "",
    chunk_text: str = "",
) -> tuple[int, int] | None:
    """The grid cell an ``[chunk, start, end]`` anchor sits in — when
    the span covers several cells (an old row-level anchor), the one
    whose own text supports ``literal``. ``None`` when no cell or an
    ambiguous match — unlocated is undecidable, never a guess
    (subset-4b)."""
    if not isinstance(span, (list, tuple)) or len(span) != 3:
        return None
    start, end = span[1], span[2]
    if not all(isinstance(x, int) and not isinstance(x, bool) for x in (start, end)):
        return None
    return _recipes._literal_cell(parsed, start, end, literal, chunk_text)


def review_table_ctx(
    measure: dict[str, Any], idx: dict[str, Any] | None
) -> tuple[dict[str, Any] | None, ParsedTable | None, str]:
    """The ``meta.table``-shaped context a ``measure_accept`` review of
    ``measure`` shows — located at review time so rows written before
    ``meta.table`` carried the header spans still get the column
    header, the row label, the caption and the units (gr506762
    subset-4).

    Returns ``(ctx, parsed, chunk_text)``: ``ctx`` merges the stored
    ``meta.table`` under the re-derived fields and carries
    ``unlocated=True`` when the source table can't be found (the
    reviewer prompt says so; the verdict stays undecidable).
    ``parsed`` feeds :func:`recipes.anchor_cell_mismatch`."""
    stored = measure.get("table_meta")
    ctx: dict[str, Any] = dict(stored) if isinstance(stored, dict) else {}
    chunk_id = measure.get("chunk_id")
    if idx is None or not isinstance(chunk_id, int):
        return (ctx or None), None, ""
    text = idx["texts"].get(chunk_id)
    if text is None:
        return (ctx or None), None, ""
    parsed = parsed_for(idx, chunk_id)
    if parsed is None:
        # A chunk that is not (or no longer) a parseable grid.
        if stored is None and idx["kinds"].get(chunk_id) != "table":
            return (ctx or None), None, text
        ctx["unlocated"] = True
        return ctx, None, text
    cell = locate_cell(
        parsed, measure.get("span"), str(measure.get("literal") or ""), text
    )
    caption = idx["captions"].get(chunk_id)
    if cell is None:
        ctx["unlocated"] = True
    else:
        r, c = cell
        orientation = ctx.get("orientation") or "columns"
        col_header = parsed.columns[c].header if 0 <= c < len(parsed.columns) else ""
        label_cell = parsed.rows[r][0].text if parsed.rows[r] else ""
        ctx.update(
            {
                "chunk": chunk_id,
                "table": idx["table_no"].get(chunk_id),
                "row": r + 1,
                "col": c + 1,
                "header": ctx.get("header")
                or (col_header if orientation == "columns" else label_cell),
                "col_header": col_header or None,
                "row_label": ctx.get("row_label") or label_cell or None,
                "header_unit": header_unit(col_header) if col_header else None,
                "row_span": list(parsed.row_spans[r])
                if r < len(parsed.row_spans)
                else None,
                "header_span": (
                    list(parsed.header_span) if parsed.header_span is not None else None
                ),
                "header_text": (
                    "\n".join(parsed.header_lines)
                    if parsed.header_span is None and parsed.header_lines
                    else None
                ),
                "footnote": parsed.rows[r][c].footnote,
                "orientation": orientation,
            }
        )
    if caption is not None:
        ctx["caption_chunk"], ctx["caption"] = caption
    elif "caption" not in ctx:
        ctx["caption"] = None
    return ctx, parsed, text


def _resolve_paper(store: Store, raw: Any) -> int | None:
    """A papers entry → a live paper ref_id (int, digit string or
    ``pa<id>`` handle)."""
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, int):
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM refs WHERE ref_id = %s AND retired_at IS NULL",
                (raw,),
            ).fetchone()
        return raw if row else None
    if isinstance(raw, str):
        text = raw.strip()
        if text.isdigit():
            return _resolve_paper(store, int(text))
        resolved = store.resolve_handle(text)
        if resolved is not None:
            return int(resolved.ref_id)
    return None


def _measurand_entries(store: Store, ref_ids: set[int]) -> dict[int, Any]:
    """``VocabEntry`` per measurand ref_id the recheck met."""
    if not ref_ids:
        return {}
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ref_id, meta, title FROM refs WHERE ref_id = ANY(%s) "
            "AND retired_at IS NULL",
            (list(ref_ids),),
        ).fetchall()
    out: dict[int, Any] = {}
    for rid, meta, title in rows:
        m = dict(meta or {})
        if not m.get("name"):
            m["name"] = str(title or "")
        out[int(rid)] = vocab_entry(int(rid), m)
    return out


def measure_recheck(
    store: Store,
    papers: list[Any],
    *,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Re-run the extraction guards over the live ``table_recipe``
    measure rows of ``papers`` (gr506762 subset-4).

    Returns ``{papers, checked, refused, unlocated, applied}``:
    ``refused`` is one entry per row that a current guard would refuse
    (each ``{id, literal, measurand, reasons: [(reason, detail)]}``);
    ``unlocated`` counts rows whose source table could not be re-parsed
    (unchecked, not refused). ``dry_run=False`` applies: a ``rejected``
    ledger review per refused row under actor ``measure-recheck`` —
    the retraction the measure kind already uses."""
    scope: list[int] = []
    for raw in papers:
        pid = _resolve_paper(store, raw)
        if pid is None:
            raise BadInput(
                f"measure recheck: papers entry {raw!r} does not resolve to a live ref",
                next="pass papers=[<paper ref_id, pa<id> or slug>, …]",
            )
        if pid not in scope:
            scope.append(pid)
    if not scope:
        raise BadInput(
            "measure recheck: no live papers given",
            next="put(kind='measure', mode='recheck', args={'papers': [<id>, …]})",
        )
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT m.id, m.measurand_ref_id, m.literal, m.reported_unit, "
            "       m.subject, m.span, m.value_form, m.value_num, "
            "       m.subject_ref_id, l.src_chunk_id, "
            "       m.meta->'table', m.meta->>'transform', "
            "       m.meta->>'extraction_run' "
            "  FROM measures m "
            "  LEFT JOIN links l ON l.link_id = m.primary_link_id "
            " WHERE m.subject_ref_id = ANY(%s) "
            "   AND m.direction = 'output' "
            "   AND m.superseded_by IS NULL "
            "   AND m.meta->'extraction'->>'prompt' = %s "
            " ORDER BY m.id",
            (scope, _recipes.RECIPE_PROMPT_VERSION),
        ).fetchall()
    indexes: dict[int, dict[str, Any]] = {}
    entries = _measurand_entries(store, {int(r[1]) for r in rows})
    refused: list[dict[str, Any]] = []
    review_only: list[dict[str, Any]] = []
    unlocated = 0
    for (
        mid,
        measurand_id,
        literal,
        unit,
        subject,
        span,
        value_form,
        value_num,
        paper,
        chunk_id,
        table_meta,
        transform,
        extraction_run,
    ) in rows:
        entry = entries.get(int(measurand_id))
        if entry is None:
            continue
        if paper not in indexes:
            indexes[int(paper)] = table_index(_paper_chunks(store, int(paper)))
        idx = indexes[int(paper)]
        parsed = parsed_for(idx, chunk_id) if chunk_id is not None else None
        text = idx["texts"].get(chunk_id, "") if chunk_id is not None else ""
        if parsed is None:
            unlocated += 1
            continue
        caption = idx["captions"].get(chunk_id)
        reasons = _recipes.stored_row_reasons(
            parsed,
            cell=locate_cell(parsed, span, str(literal or ""), text),
            literal=str(literal or ""),
            unit=unit,
            subject=subject,
            value_form=value_form,
            value_num=value_num,
            entry=entry,
            table_meta=table_meta if isinstance(table_meta, dict) else {},
            transform=transform,
            caption_text=caption[1] if caption else "",
            chunk_text=text,
            span=span,
        )
        if reasons:
            entry_ = {
                "id": int(mid),
                "paper": int(paper),
                "literal": literal,
                "measurand": entry.name,
                "extraction_run": extraction_run,
                "reasons": reasons,
            }
            # anchor_cell_mismatch is judged against today's re-parse —
            # a stored row whose grid changed since it was written trips
            # it while being correct (parser drift, subset-4c). Those
            # rows go to a review-only group and never apply.
            if all(r == "anchor_cell_mismatch" for r, _d in reasons):
                review_only.append(entry_)
            else:
                refused.append(entry_)
    applied = 0
    if refused and not dry_run:
        for row in refused:
            detail = "; ".join(f"{r}: {d[:120]}" for r, d in row["reasons"])
            try:
                store.record_target_review(
                    "measure",
                    row["id"],
                    actor="measure-recheck",
                    verdict="rejected",
                    version="1",
                    note=f"extraction-guard recheck: {detail}"[:500],
                )
            except Exception:
                continue
            applied += 1
    return {
        "papers": scope,
        "checked": len(rows),
        "refused": refused,
        "review_only": review_only,
        "unlocated": unlocated,
        "applied": applied,
    }


def _id_ranges(ids: list[int]) -> str:
    """``mx1-3, mx7, mx9-10`` — consecutive ids compressed to ranges."""
    ordered = sorted(set(ids))
    parts: list[str] = []
    i = 0
    while i < len(ordered):
        j = i
        while j + 1 < len(ordered) and ordered[j + 1] == ordered[j] + 1:
            j += 1
        parts.append(f"mx{ordered[i]}-{ordered[j]}" if j > i else f"mx{ordered[i]}")
        i = j + 1
    return ", ".join(parts)


def _detail_line(row: dict[str, Any]) -> str:
    mx = handle_registry.format_handle("measure", row["id"])
    why = "; ".join(r for r, _d in row["reasons"])
    return (
        f"  {mx} pa{row['paper']} {row['measurand']} "
        f"run={row.get('extraction_run') or '-'} [{row['literal']}]: {why}"
    )


def recheck_lines(
    result: dict[str, Any], *, detail: bool = False, page: int = 1, page_size: int = 50
) -> list[str]:
    """The response text for a recheck call.

    Default is the compact summary — counts by reason × paper ×
    extraction_run plus the refused ids grouped by reason as id
    ranges — so a multi-paper run fits one reply (subset-5: the
    24-paper dry run's per-row listing truncated at ~24 kB).
    ``detail=True`` pages the per-row listing, ``page_size`` rows a
    page."""
    lines = [
        f"measure recheck over {len(result['papers'])} paper(s): "
        f"{result['checked']} table_recipe row(s) checked, "
        f"{len(result['refused'])} would now be refused"
        + (
            f", {len(result['review_only'])} review-only"
            if result["review_only"]
            else ""
        )
        + (
            f", {result['unlocated']} unlocated (unchecked)"
            if result["unlocated"]
            else ""
        )
    ]
    if not detail:
        counts: dict[tuple[str, int, str], int] = {}
        by_reason: dict[str, list[int]] = {}
        for row in result["refused"]:
            run = str(row.get("extraction_run") or "-")
            for reason, _d in row["reasons"]:
                key = (reason, row["paper"], run)
                counts[key] = counts.get(key, 0) + 1
                by_reason.setdefault(reason, []).append(row["id"])
        if counts:
            lines.append("counts by reason × paper × extraction_run:")
            for (reason, paper, run), n in sorted(counts.items()):
                lines.append(f"  {reason}  pa{paper}  {run}  {n}")
            lines.append("refused ids by reason (a row can carry several):")
            for reason in sorted(by_reason):
                ids = by_reason[reason]
                lines.append(f"  {reason}: {_id_ranges(ids)} ({len(set(ids))})")
        if result["review_only"]:
            ro_reasons: dict[str, list[int]] = {}
            for row in result["review_only"]:
                for reason, _d in row["reasons"]:
                    ro_reasons.setdefault(reason, []).append(row["id"])
            lines.append("review-only (parser-drift-possible — never auto-applied):")
            for reason in sorted(ro_reasons):
                ids = ro_reasons[reason]
                lines.append(f"  {reason}: {_id_ranges(ids)} ({len(set(ids))})")
    else:
        rows = result["refused"]
        total = len(rows)
        pages = max(1, -(-total // page_size))
        page = min(max(1, page), pages)
        start = (page - 1) * page_size
        lines.append(f"detail page {page}/{pages} — {total} refused row(s):")
        for row in rows[start : start + page_size]:
            lines.append(_detail_line(row))
        if page < pages:
            lines.append(
                f"  … {total - start - page_size} more — "
                f"args={{'detail': True, 'page': {page + 1}, "
                f"'page_size': {page_size}}}"
            )
        if result["review_only"]:
            ro_rows = result["review_only"]
            ro_total = len(ro_rows)
            ro_pages = max(1, -(-ro_total // page_size))
            ro_page = min(max(1, page), ro_pages)
            ro_start = (ro_page - 1) * page_size
            lines.append(
                f"  review-only page {ro_page}/{ro_pages} — {ro_total} "
                "row(s) (parser-drift-possible — never auto-applied):"
            )
            for row in ro_rows[ro_start : ro_start + page_size]:
                lines.append(_detail_line(row))
            if ro_page < ro_pages:
                lines.append(
                    f"  … {ro_total - ro_start - page_size} more — "
                    f"args={{'detail': True, 'page': {ro_page + 1}, "
                    f"'page_size': {page_size}}}"
                )
    if result["applied"]:
        lines.append(
            f"applied: {result['applied']} rejected review(s) recorded — "
            "the rows drop out of live rankings"
        )
    return lines


__all__ = [
    "locate_cell",
    "measure_recheck",
    "parsed_for",
    "recheck_lines",
    "review_table_ctx",
    "table_index",
]
