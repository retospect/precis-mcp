"""Prompt text for the survey extraction pass.

Two prompts, two models:

* :func:`reader_prompt` — the per-chunk reader prompt for the local
  model: the measurand vocabulary it may name, then the chunk rendered
  by :func:`precis.extract.anchors.anchored_text` with ``[n]`` markers,
  then the exact reply contract. Kept deliberately small — the reader
  proposes bindings, the deterministic checks and the frontier sampler
  carry the burden of proof.
* :func:`review_prompt` — the per-measure sampler prompt for the
  frontier model: one written row plus the text of its anchored span;
  the verdict the acceptance step counts.
"""

from __future__ import annotations

from typing import Any

from precis.extract.anchors import anchored_text
from precis.extract.proposals import VocabEntry
from precis.extract.tables import ParsedTable
from precis.utils.llm.json_reply import extract_json_object

_READER_CONTRACT = """\
Reply with ONE JSON object and nothing else:
{"results": [ ... ]}   or   {"results": []} when the chunk reports none.

Each result:
{
 "subject": "<paper-local label for the catalyst/sample/system, or null>",
 "group": "<finding or table name these results share, or null>",
 "measurand": "<a measurand slug from the list below, or null>",
 "literal": "<the exact printed string: '95', '<1', '550-575', 'NH3'>",
 "unit": "<the printed unit, or null>",
 "anchor": "<the [n] sentence the literal is printed in: '4' or '4-6'>",
 "attribution": "own_work | cited_work | not_established",
 "tier": "measured | computed | asserted | null",
 "measurand_status": "explicit | interpreted | ambiguous | null",
 "reference": "<electrode reference etc., or null>",
 "normalization": "<what the number is normalized by, or null>",
 "value_form": "categorical | null",
 "bound": "upper | lower | null",
 "sign": -1,
 "range": "<the bracketed spread printed by the value, verbatim, or null>",
 "conditions": {"<name>": "<value as printed>"},
 "inputs": [ ... ]
}

attribution: own_work = these authors produced the number; cited_work =
restated from another paper; not_established = unattributed.
tier: measured = their experiment, computed = their model/DFT,
asserted = stated without established origin. null when unsure.
value_form "categorical" only for a non-numeric literal (a product
name, a substrate, a phase).
bound: the literal is a one-sided bound, not a point — "upper" when the
sentence caps it ('at most', 'less than', 'no more than'), "lower" when
it floors it ('at least', 'exceeding', 'more than').
sign: -1 only when the sentence frames the printed literal as a
decrease ('cools by', 'reduces', 'X% below the baseline'); the literal
still copies the printed digits.
range: a bracketed model spread or interval printed with the value —
'0.96 [-0.17 to -1.21]' -> literal '0.96', range '[-0.17 to -1.21]'.
conditions: what the number is relative to — baseline or scenario,
period, model or ensemble, region — as name: printed-value pairs, when
no measurand in the list names them (else use an input).

Each input is a condition of THIS result:
{"condition": "<condition name, e.g. product, applied-potential,
electrolyte-concentration>", "measurand": "<slug>", "literal": "<exact
printed string>", "unit": "<or null>", "anchor": "<[n] if elsewhere in
the chunk>", "role": "context|preparation|model", "value_text": "<for a
categorical condition, e.g. 'NH3'>"}

Rules:
- a result is a number THESE authors produced for the paper's own
  subject — its intervention, experiment, model or synthesis. A value
  belonging to another work or a named comparator — a cited threshold
  or target, another technology's cost or performance, a scenario's
  baseline level, a bound on the difference between two models — is
  context, not a result: attach it as an input with role "context" and
  attribution "cited_work" of the result it conditions, or omit it.
  attribution "cited_work" on a result is dropped;
- a bound keeps its direction: 'at most 0.2' -> literal '0.2' with
  "bound": "upper", 'exceeding 100,000' -> "bound": "lower". Never
  write a bounded value as a bare point;
- a change the sentence frames as a decrease is negative: 'cools by
  0.9' -> literal '0.9' with "sign": -1; 'emits 15% below the
  baseline' -> literal '15', "sign": -1, "conditions":
  {"baseline": "<the baseline as printed>"};
- a bracketed spread goes verbatim to "range";
- in a table row, a cell holding only '-' signs the number in the next
  cell — the literal carries it ('-1.93' for '| - | 1.93 |');
- the literal must be copied EXACTLY as printed, sign and dashes
  included — markup is notation, not content: '$-0.7 \\pm 0.1
  \\text{ W m}^{-2}$' is literal '-0.7 ± 0.1' with unit 'W m-2',
  '<sup>−</sup>1.9' is '-1.9', and a value split across table cells or
  lines is still one number;
- anchor to the sentence that prints the literal, nothing wider;
- do not invent numbers, units or measurands — emit [] rather than guess;
- file a number only under a measurand whose meaning matches it; when the
  chunk states a real number no listed measurand describes — a share, a
  relative change, a figure the list has no taxon for — emit
  "measurand": null; the closest slug is not a fallback;
- a measurand not in the list cannot be named.\
"""

_RECIPE_CONTRACT = """\
Reply with ONE JSON object and nothing else — the mapping recipe for
THIS table — or {"table": null, "reason": "glossary|setpoints|no results"}
when the table reports no measured values at all (a glossary, a
setpoint list, a units key, a figure caption restated as a table).

{"table": {
  "orientation": "columns | rows",
  "subject_axis": <0-based index of the column ("columns") or row
    ("rows") holding the subject labels; null when the column headers
    themselves are the subjects>,
  "group": "<table name these results share, or null>",
  "attribution": "own_work | cited_work | not_established",
  "tier": "measured | computed | asserted | null",
  "conditions": [
    {"condition": "<name>", "measurand": "<slug>",
     "literal": "<the exact printed string>", "unit": "<or null>",
     "role": "context | preparation | model",
     "source": "caption | table", "cell": "r:c"}
  ],
  "series": [
    {"index": <0-based column ("columns") or row ("rows") index>,
     "measurand": "<slug>", "unit": "<printed unit or null>",
     "unit_index": <series index carrying the per-cell unit>,
     "role": "output | input", "condition": "<condition name>",
     "transform": "log10 | ln",
     "attribution": "<as above>", "tier": "<as above>"}
  ],
  "skip_cells": ["r:c", ...],
  "overrides": {"r:c": {"measurand": ..., "measurand_status": ...,
    "reference": ..., "normalization": ..., "unit": ...}}
}}

Rules:
- every index is a 0-based GRID coordinate: a column number for
  "columns", a row number for "rows"; "r:c" keys name a cell by its
  0-based row and column;
- "columns" means each series is a column and each row is one subject;
  "rows" means each series is a row and each column is one subject;
- one series per measurable axis. role "input" marks a condition axis
  (a temperature column that conditions the row's outputs, not a
  result); "output" marks a result axis. The label column / label row
  is not a series — list its cells in skip_cells;
- unit is the unit PRINTED for the series — the header's unit hint is
  the parser's guess, check it against the cell values. null is allowed
  only for a measurand whose canonical unit is 1 (dimensionless).
  unit_index points at a series carrying a per-cell unit instead;
- transform "log10"/"ln" means the cells print the log of the value
  ('log ν = 13' stores 10^13) — still name the measurand's real unit;
- conditions are the table-level inputs every output shares — a
  temperature the caption states, a pressure given for the whole table.
  source "caption" anchors the literal inside the caption text; source
  "table" anchors it in a cell and needs "cell": "r:c";
- skip_cells names cells that are not readings (footer lines, totals,
  notes the grid kept as cells);
- overrides repair ONE cell's interpretation — measurand,
  measurand_status, reference, normalization, unit — never the literal;
- attribution/tier on the table are defaults a series may override; a
  column or row whose header or label carries a citation marker is
  cited_work whatever you claim — the parser detects it;
- you never copy a literal: the parser reads every cell verbatim.
  Name only measurands from the list — an axis with no fitting slug is
  left out of the recipe, not mapped to the closest one.\
"""


def _vocab_lines(vocab: list[VocabEntry]) -> list[str]:
    lines = ["Measurands you may name (slug — canonical unit):"]
    for entry in vocab:
        unit = entry.display_unit or entry.canonical_unit or "-"
        req = (
            f" (needs: {', '.join(entry.required_conditions)})"
            if entry.required_conditions
            else ""
        )
        lines.append(f"- {entry.slug} — {unit}{req}")
    return lines


def reader_prompt(
    *,
    vocab: list[VocabEntry],
    chunk_text: str,
    section: str | None = None,
    paper_title: str | None = None,
) -> str:
    """One chunk's reader prompt for the local extraction model."""
    lines = [
        "You are reading one chunk of a survey paper for a numeric-extraction",
        "pass. Extract every numeric or categorical result the chunk states",
        "about the paper's own catalyst, material, electrode, reaction",
        "system or finding — each with the sentence it is printed in and",
        "the conditions it was taken under. Context numbers (cited values,",
        "comparators, scenario baselines, thresholds) are not results.",
    ]
    if paper_title:
        lines.append(f"Paper: {paper_title}")
    if section:
        lines.append(f"Section: {section}")
    lines.append("")
    lines.extend(_vocab_lines(vocab))
    lines.append("")
    lines.append(_READER_CONTRACT)
    lines.append("")
    lines.append("Chunk (sentences marked [n]):")
    lines.append(anchored_text(chunk_text))
    return "\n".join(lines)


def table_recipe_prompt(
    *,
    parsed: ParsedTable,
    caption: tuple[int, str] | None,
    vocab: list[VocabEntry],
    paper_title: str | None = None,
) -> str:
    """One parsed table's recipe prompt — the ``survey_extract:table_recipe``
    op. The model sees the *grid*, not the raw markdown: numbered columns
    with their headers, unit hints and citation flags, then numbered rows
    with per-column cells. Its reply maps axes to measurands; the cells
    themselves are read verbatim by :func:`precis.extract.recipes.expand_recipe`."""
    lines = [
        "You are mapping one parsed table of a survey paper for a "
        "numeric-extraction pass. The grid below is already parsed into "
        "columns and rows — your job is to name which column (or row) "
        "carries which measurand, the printed unit, the subject axis and "
        "the table-level conditions. You never copy a value: every cell "
        "is read verbatim by the parser.",
    ]
    if paper_title:
        lines.append(f"Paper: {paper_title}")
    lines.append("")
    lines.extend(_vocab_lines(vocab))
    lines.append("")
    cap_text = caption[1] if caption is not None else None
    if cap_text:
        lines.append(f"Caption: {cap_text}")
    else:
        lines.append("Caption: (none paired to this table)")
    lines.append("")
    lines.append("Columns (index — header [unit hint] [cited]):")
    for col in parsed.columns:
        marks = ""
        if col.header_unit:
            marks += f" [{col.header_unit}]"
        if col.cited:
            marks += " [cited]"
        lines.append(f"- col {col.index}: {col.header or '(empty)'}{marks}")
    lines.append("")
    lines.append("Rows (index — cells, [col] text):")
    for i, row in enumerate(parsed.rows):
        cells = " | ".join(f"[{c}] {cell.text}" for c, cell in enumerate(row))
        lines.append(f"- row {i}: {cells}")
    lines.append("")
    lines.append(_RECIPE_CONTRACT)
    return "\n".join(lines)


def parse_reply(text: str) -> dict[str, Any] | None:
    """The model's reply as a dict, or None on unparseable output."""
    return extract_json_object(text)


def review_prompt(
    *,
    literal: str,
    reported_unit: str | None,
    measurand: str,
    subject_label: str | None,
    span_text: str,
    conditions: list[str] | None = None,
    table: dict[str, Any] | None = None,
    transform: str | None = None,
    stored_value: Any = None,
) -> str:
    """The per-row sampler prompt: is this row what the span says?

    ``table`` carries the row's ``meta.table`` context (recipe-extracted
    rows only) — its ``T<n>`` number, 1-based row/col, the series label
    as ``header``, the ``orientation`` and a resolved ``caption`` text —
    without it the reviewer sees one cell with no column name.

    ``transform`` is the row's ``meta.transform`` (``log10``/``ln``): the
    cell prints the log of the value, so the literal is the exponent —
    without the note a log-scale row reads as a misreported unit and the
    reviewer rejects a correct row (gr506762 slice-4, mx1410/mx1424).
    ``stored_value`` is the row's transformed ``value_num``."""
    bits = [f"measurand: {measurand}", f"literal: {literal}"]
    if reported_unit:
        bits.append(f"reported unit: {reported_unit}")
    if subject_label:
        bits.append(f"subject: {subject_label}")
    for c in conditions or []:
        bits.append(f"condition: {c}")
    if transform:
        stored = (
            f"; the stored value is {stored_value:g}"
            + (f" {reported_unit}" if reported_unit else "")
            if stored_value is not None
            else ""
        )
        bits.append(
            f"transform: the cell prints {transform}(value) — the literal "
            f"is the printed exponent, not the value itself{stored}"
        )
    if table:
        coord = (
            f"T{table.get('table')} row {table.get('row')} "
            f"col {table.get('col')} ({table.get('orientation') or 'columns'})"
        )
        bits.append(f"table cell: {coord}")
        if table.get("header"):
            bits.append(f"series label: {table['header']}")
        if table.get("caption"):
            bits.append(f"table caption: {table['caption']}")
        if table.get("footnote"):
            bits.append(f"footnote marker: {table['footnote']}")
    return (
        "A numeric-extraction pass wrote this measure row from a survey "
        "paper. The anchored source span follows the row. Reply with ONE "
        "JSON object and nothing else:\n"
        '{"verdict": "correct|wrong|undecidable", "reason": "<short>"}\n\n'
        "correct = the literal, unit and measurand say what the span says, "
        "about the stated subject. wrong = the value is misread, the unit "
        "wrong, the measurand misassigned, or the span does not contain the "
        "literal. undecidable = the span is there but the claim cannot be "
        "judged from it.\n\n" + "\n".join(bits) + "\n\nspan:\n" + span_text
    )


def parse_verdict(text: str) -> tuple[str, str | None]:
    """The sampler's verdict: ``('correct'|'wrong'|'undecidable', reason)``.

    An unparseable or unlabeled reply reads as ``('undecidable', ...)`` —
    never silently a pass.
    """
    obj = extract_json_object(text)
    if not isinstance(obj, dict):
        return "undecidable", "unparseable reply"
    verdict = obj.get("verdict")
    reason = obj.get("reason") if isinstance(obj.get("reason"), str) else None
    if verdict in ("correct", "wrong", "undecidable"):
        return verdict, reason
    return "undecidable", reason
