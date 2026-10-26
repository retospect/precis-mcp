---
status: draft
title: Structured numeric extraction from paper tables — parsed table view, table recipes into measures, paper/quest-scoped measure queries (gr506762)
pillar: memory-graph
prio: high
model: sonnet
---

# Structured numeric extraction from paper tables (gr506762)

## Motivation / why

Gripe gr506762, filed by quest qu500066 (surface-kinetics engine, rubric:
"sticking coefficients, prefactors and barriers each grounded in a cited
source"): search ranking, `get(kind='paper')` chunk reads and finding
full text all stop at prose and snippets. None of them returns the numbers
in a paper's results tables. The quest wants sticking coefficients,
prefactors and barriers back as numbers, each with value, unit,
uncertainty, conditions and an anchor to the source table.

What exists, so this item reuses it and does not rebuild it:

- **Tables are already parsed and stored.** Marker emits markdown pipe
  tables, `ingest/marker.py::_classify_chunk` types them
  `chunk_kind='table'`, `ingest/text_chunker.py::split_table` splits long
  ones and repeats the header, and
  `ingest/marker.py::_reconcile_continuation_table_headers` repairs
  continuation headers. Table chunks are embedded (`workers/embed.py`
  skips only `references` and `field`). There are no column metadata and
  no caption link: `_match_captions` pairs captions with figures only.
- **The number store exists.** `measures` (0187) holds a literal-first,
  SI-normalised, append-only row per value. It has `value_err`,
  `value_form`, `tier`, `source_attribution`, `measurand_status`,
  computed `extraction_status`, input rows for conditions, and a
  raw-offset anchor on a paper chunk. Reads are `search(kind='measure',
  property=, min=, max=, unit=, q=)`, `get(kind='measure')` and
  `get(kind='quest', view='measures')` (contract: skill
  `precis-measure-help`).
- **The extraction pass exists.** The `survey_extract` coordinator job
  (`workers/job_types/survey_extract.py`) reads `paragraph` and `table`
  chunks, one LLM call per chunk, and writes checked runs through
  `store.insert_measure`. The `measure_accept` child signs off each batch
  by Wilson-rule sampling. On a table it asks for one JSON result per cell
  under `max_tokens`, so a 10×3 table costs about 30 results and
  truncates. `corpus-quantitative-extraction.md` slice T1 specified the
  fix, a per-table recipe that a pure module expands. This item absorbs
  T1 and builds it.
- **What is missing for the quest agent:** a way to see one paper's
  tables as grids, a table-only search, a measure search scoped to one
  paper or quest, and surface-kinetics measurand taxa. Only `activation
  barrier` (tn465832) and `adsorption energy` (tn465830) exist.

Measured on prod on 2026-10-25 (read-only): 351 papers serve qu500066,
262 have a body and 143 have tables. Those papers hold 1,516 table
chunks, 1,406 of them pipe-delimited, and 765 mention a kinetic term
(sticking, prefactor, barrier, desorption, kJ, eV). They have 0 measures.
Table chunks: median 1,367 chars, p90 1,588; 638 carry HTML markup
(`<br>`, `<sup>`). A `Table N` caption chunk sits within two chunks
before the table for 134 of them and within two after for 79. Two sample
tables show the shapes this spec has to handle:

```
| | Oxidation rate constant $K_i N_0 (L^{-2})$ | Saturation coverage $\theta_{\rm sat}$ (ML) | ... | Saturation sticking $s_{\rm rev}$ |
| Ni{100} | 0.0002 | 3.6 | 0.68 | 0.062 |
```

```
| | expt | | |
| | Wichtendahl et al. <sup>56</sup> | Dohnalek et al. <sup>44</sup> | |
| pre-exponential, $\log \nu$ (s <sup>-1</sup> ) | 13 | $15 \pm 2$ | |
| $E_{\rm d}^{\;\rm A}$ | $13.5 \pm 1.1^a$ | $18.5 \pm 2.0^{b}$ | |
```

The second sample has measurands in rows and sources in columns
(transposed), cites other papers' values, gives a log-scale prefactor
and puts footnote markers on the uncertainties.
`taxonomy/measure_units.py::parse_literal` reads `13.5 \pm 1.1^a` as
categorical, because `markup_surface` folds it to `13.5 ± 1.1a`.

## Decisions

1. **Source: existing parse output, no new PDF extraction.** Every slice
   reads `chunks` rows with `chunk_kind='table'`. Nothing re-ingests,
   and body chunks stay append-only. A table that is not a well-formed
   pipe grid (110 of 1,516 here; collapsed OCR runs) is shown raw and
   never guessed at. This repeats the boundary in
   `corpus-quantitative-extraction.md`.
2. **Storage: `measures`, no migration.** Extracted values are ordinary
   `measures` runs written through `insert_measure`. Parsed grids are
   computed when read, because a paper has few tables and parsing is
   cheap. A raw `table_cells` store was rejected. It would hold numbers
   with no measurand, unit or trust state (about 124k tables corpus-wide,
   or about 4M rows), and it would be a second number store beside the
   one 0187 consolidated. The cell provenance rides in the frozen
   `measures.meta` at insert.
3. **Deterministic grid, LLM mapping.** Splitting a table into header,
   rows, cells, header units, caption, footnote markers and citation
   markers is pure code, `extract/tables.py`. The LLM is used for one
   job only: the recipe. It maps columns (or rows) to measurand taxa and
   units, names the subject axis, the table-level conditions and the
   attribution. That is one call per table chunk, and the reply grows
   with the number of columns, not cells. Every value is still the exact
   cell text, checked against its cell span, and parsed by
   `parse_literal`, never by the model.
4. **Model and queue.** The recipe call is a new steerable op,
   `survey_extract:table_recipe`, `OpDefault(tier=Tier.MEDIUM)`. A
   one-shot JSON mapping of about 10 headers needs only a haiku-class
   model. It runs inside the existing `survey_extract` coordinator job
   with the job's placement. The up-front `placement_refusal` check
   covers both ops. There is no new worker, lane or queue. Acceptance
   reuses `measure_accept` unchanged.
5. **Anchor = the cell, not the row.** Each output's anchor is the raw
   offset span `[chunk, cell_start, cell_end]` of its cell. T1 said the
   row span. Two columns in one row can print the same literal for the
   same measurand (one value at two temperatures). With a row anchor,
   `insert_measure`'s duplicate refusal (same measurand, subject, span
   and value) would drop the second value. `meta.table` keeps the row
   span and the header for the sampler and the renderer.
6. **Attribution is detected, not trusted from the model.** A column or
   row whose header or label cell carries a citation marker (`et al.`,
   `<sup>56</sup>`, `[12]`, `ref.`) is `cited_work` → `tier='asserted'`,
   whatever the recipe says. Comparison tables of literature values are
   common in this domain, and 0187's guard already refuses
   `cited_work` + `measured`.
7. **Query surface.** Slice 1 adds `get(kind='paper', id=, view='tables')`
   and `search(kind='paper', args={'chunk_kind': 'table'})`. Slice 3
   adds `search(kind='measure', args={'paper': …} | {'serves': …})` and
   `get(kind='paper', id=, view='measures')`. `get(kind='quest',
   view='measures')` already exists.

## Slice 1 — parsed table view and table-only search (deterministic, no LLM, no migration)

Useful alone: from the day it deploys, a quest agent can find the tables
that mention a term across a quest's papers and read each one as a grid,
with the units from the header, the caption and a chunk anchor it can
cite. No extraction run is needed first.

### 1a. `src/precis/extract/tables.py` (new, pure, stdlib + `precis.utils.text`)

```python
@dataclass(frozen=True, slots=True)
class Cell:
    text: str            # exact cell text, verbatim slice of the chunk
    start: int           # offset of text in chunk.text
    end: int
    value_text: str      # text with a trailing footnote marker removed
    footnote: str | None # 'a', 'b', '*', '†' … or None
    numeric: bool        # parse_literal(value_text).form not in ('categorical',)

@dataclass(frozen=True, slots=True)
class Column:
    index: int
    header: str              # header cell(s) joined with ' / ' for a 2-row header
    header_unit: str | None  # deterministic unit guess from the header, see below
    cited: bool              # header carries a citation marker

@dataclass(frozen=True, slots=True)
class ParsedTable:
    chunk_id: int
    columns: list[Column]
    rows: list[list[Cell]]           # data rows only, separator dropped
    row_spans: list[tuple[int, int]] # offsets of each data row line
    header_rows: int                 # 0, 1 or 2
    ragged_rows: list[int]           # data-row indices whose cell count != header's
    row_label_cited: list[bool]      # per row: label cell carries a citation marker

def parse_table(chunk_id: int, text: str) -> ParsedTable | None: ...
def header_unit(header: str) -> str | None: ...
def strip_footnote(cell: str) -> tuple[str, str | None]: ...
def has_citation_marker(cell: str) -> bool: ...
def assign_captions(tables: Sequence[tuple[int, int]], neighbours: Sequence[tuple[int, int, str]]) -> dict[int, tuple[int, str]]: ...
```

Rules:

- **Grid.** Lines that start with `|`. A separator line
  (`ingest/marker.py::_is_table_separator` semantics; reuse
  `extract/proposals.py::_table_line_cells` / `_is_table_separator`, or
  lift them here and import them back into `proposals.py`, so there is
  one definition) marks the end of the header. With no separator, row
  0 is the header. A second header row counts when it sits before the
  separator, or when row 1 has no numeric cell and row 0 has empty
  cells (the sample-2 shape). `parse_table` returns `None` when fewer
  than 2 lines start with `|`.
- **Cell offsets** are exact. `chunk.text[cell.start:cell.end] ==
  cell.text` always holds, and the text is stripped of surrounding
  spaces. Slice 2's anchors depend on this.
- **Footnote.** A trailing `^a`, `^{b}`, `<sup>a</sup>`, `*`, `†` or `‡`
  after a number is removed into `footnote`. Only a single letter or
  symbol counts. `^{-2}` and `<sup>-1</sup>` are exponents and stay.
- **Header unit.** Take the last parenthesised or bracketed group of
  the header after `markup_unit`, e.g. `(s <sup>-1</sup> )` → `s^-1`,
  `(ML)` → `ML`, `[kJ/mol]` → `kJ/mol`. Keep it only when
  `taxonomy/measure_units` can parse it as a unit, or it is one of the
  dimensionless labels `ML`, `%` or `1`. Otherwise it is `None`. It is a
  hint for display and the recipe, never a stored unit on its own.
- **Citation marker.** `has_citation_marker` is true for `et al`,
  `<sup>\d+(,\d+)*</sup>`, `^{\d+}`, `[\d+(,\d+)*]` and `ref.`/`refs.`
  (case-insensitive).
- **Caption.** `assign_captions` pairs tables with caption chunks
  one-to-one across the ref: candidates open `Table <n>` followed by
  `.`, `:` or a closing `**` (so `Table 2 shows…` prose is out), sit
  within ord −2…+2, and a numbered caption only attaches to the table
  with that running number. Greedy by distance, numbered matches ahead
  of unnumbered, ties going to the caption before the table; an
  unpaired table gets no caption — never a neighbour's. Returns
  `{table_chunk_id: (caption_chunk_id, first 300 chars)}`.

### 1b. `get(kind='paper', id=X, view='tables')`

In `handlers/paper.py`: add `"tables"` to `_SUPPORTED_VIEWS`, add a
`view == "tables"` branch beside `"chunks"`, and put the render in a new
`handlers/_paper_tables.py` (paper.py is already 6k lines). Declare
`"get": ArgsSpec({"match", "table"})` in `PaperHandler.ARGS` if get args
are gated, and check how `view='chunks'` receives args.

- One store read: the paper's live body chunks (`ord >= 0`,
  `retired_at IS NULL`) of kind `table`, plus their ord ±2 neighbours
  for captions. Add `ChunksMixin.table_chunks_for_ref(ref_id)` in
  `store/_chunks_ops.py`, returning `(chunk_id, ord, text,
  section_path)` for the tables and `(chunk_id, ord, text)` for the
  neighbours.
- Render per table, in ord order:

  ```
  ## T2 · pc180352 · §3.1 Oxidation kinetics
  caption (pc180351): Table 2. Kinetic parameters for O2 on Ni single crystals at 300 K …
  columns: [2] Oxidation rate constant K_i N_0 (L^-2) · [3] Saturation coverage θ_sat (ML) [unit ML] · … · [5] Saturation sticking s_rev
  | row | label | [2] | [3] | [4] | [5] |
  | 1 | Ni{100} | 0.0002 | 3.6 | 0.68 | 0.062 |
  …
  measures: 0 extracted · cite a cell as pc180352 row 1 col 5
  ```

  Headers and cells are shown through `markup_surface` for reading. A
  `ragged` row is marked and a `cited` column carries `(cited)`. A
  table that `parse_table` rejects prints `unparsed — raw text:` and
  the first 600 chars.
- `T<n>` is the table's 1-based position among this paper's table
  chunks, and `args={'table': n}` (or a chunk handle) shows only that
  one. `args={'match': 'sticking prefactor'}` keeps tables whose
  header, caption or row labels contain any of the words
  (case-insensitive, on the `markup_surface` text).
- Budget: render at most 12 tables. After that, add one line
  `… N more: args={'table': k}` per hidden table, so the fold never
  slices a table in half.
- With no table chunks, the body is a one-liner and the next step names
  `view='chunks'`. On a stub (no body), refuse the same way the other
  body views do.
- The measure count per table (slice 3 fills it) is a count of live
  `measures` whose anchor chunk is that table. Until slice 3 it shows
  `0`, so the line is already there.

### 1c. `search(kind='paper', q=…, args={'chunk_kind': 'table'})`

Add a `chunk_kind: str | list[str] | None` kwarg to `PaperHandler.search`,
next to `serves` / `has_fulltext`. Validate it against `chunk_kinds.slug`.
An unknown value is `BadInput` with `options=` set to the body kinds
(`paragraph`, `table`, `caption`, `equation`, `figure`). Thread it to the
`chunk_kinds=` parameter that `store/_chunks_ops.py` already takes on
both the lexical and the semantic path (`_chunk_scope_clauses`), through
the fused search in `handlers/_paper_search.py`. It implies
`unique_per='chunk'`, the drill shape, as `scope=` does. It composes with
`args={'serves': 'quest:500066'}`.

### Slice 1 acceptance

1. `parse_table` on the two sample shapes above (fixtures inline in the
   test, written by hand, not copied prod text) gives the right column
   count, header text, `header_unit` (`L^-2` for sample 1 column 1,
   because pint parses it as a hint even though it means Langmuir; `ML`;
   `s^-1` for sample 2), the 2-row header for sample 2, `footnote='a'` on
   `13.5 \pm 1.1^a`, `numeric=True` for that cell, and `cited=True` on
   both source columns of sample 2. Every cell satisfies
   `text[start:end] == cell.text`.
2. A ragged table lists the bad row in `ragged_rows`. A newline-free
   collapsed run returns `None`.
3. On the dev DB, a paper with 3 table chunks (one with a caption chunk
   before it, one unparseable) renders 3 sections. The caption is
   attached to the right one, the unparseable one is shown raw, and
   `args={'match': 'sticking'}` keeps exactly the one whose header says
   it.
4. `search(kind='paper', q='sticking', args={'chunk_kind': 'table'})`
   returns only table-chunk hits, while the same query without the arg
   also returns a paragraph hit. `args={'chunk_kind': 'tabel'}` is
   refused with `options=`.
5. `precis-paper-help` documents `view='tables'` and the `chunk_kind`
   arg. `get(kind='skill', id='precis-paper-help')` lists the view.

Tests: `tests/test_extract_tables.py` (pure, AC 1–2),
`tests/test_paper_tables_view.py` (dev DB, AC 3–4). Run them with
`scripts/test tests/test_extract_tables.py tests/test_paper_tables_view.py`.

## Slice 2 — table recipes into measures (absorbs corpus-quantitative-extraction T1)

### 2a. Recipe contract (reader reply for one table chunk)

The prompt is `extract/prompt.py::table_recipe_prompt(parsed, caption,
vocab, paper_title)`. It shows the parsed grid with column numbers and
row numbers (not the raw markdown), the caption, the `header_unit`
hints, the measurand vocabulary in the same form as `reader_prompt`, and
this contract:

```json
{"table": {
  "orientation": "columns",
  "subject_axis": 0,
  "group": "Table 2",
  "attribution": "own_work",
  "tier": "measured",
  "conditions": [{"condition": "temperature", "measurand": "<slug>",
                  "literal": "300 K", "role": "context",
                  "source": "caption"}],
  "series": [
    {"index": 4, "measurand": "<slug>", "unit": null, "role": "output"},
    {"index": 2, "measurand": "<slug>", "unit": "ML", "role": "output"},
    {"index": 1, "measurand": "<slug>", "unit_index": 5, "role": "output"},
    {"index": 3, "measurand": "<slug>", "unit": "K", "role": "input",
     "condition": "temperature"},
    {"index": 2, "measurand": "<slug>", "unit": "s^-1", "role": "output",
     "transform": "log10"}
  ],
  "skip_cells": ["3:2"],
  "overrides": {"5:3": {"measurand_status": "ambiguous"}}
}}
```

- `orientation`: `"columns"` means each series is a column and each
  subject a row (sample 1). `"rows"` means each series is a row and each
  subject a column (sample 2). `subject_axis` is the column (or row)
  index holding the subject labels. `index` is a column index for
  `"columns"` and a row index for `"rows"`.
- `unit` is the printed unit, or `unit_index` points at the series
  carrying the unit per cell. `null` is allowed only for a measurand
  whose canonical unit is `1`.
- `transform`: `"log10"` or `"ln"`. The cell prints the log of the
  value (`log ν = 13`).
- `attribution` / `tier` are table-level defaults that a series may
  override. Decision 6 overrides both.
- `conditions[].source`: `"caption"` (anchored in the caption chunk) or
  `"table"` (anchored in a cell, given as `"cell": "r:c"`).
- `overrides`: keyed `row:col` in grid coordinates. It may change
  `measurand`, `measurand_status`, `reference`, `normalization` and
  `unit`, never `literal`.
- `{"table": null, "reason": "<glossary|setpoints|no results>"}` is a
  valid reply. A setpoint or glossary table writes nothing and counts
  `skip:no_results`.

### 2b. `src/precis/extract/recipes.py` (new, pure)

```python
def expand_recipe(
    parsed: ParsedTable, recipe: dict[str, Any], *, chunk_id: int,
    caption: tuple[int, str] | None, vocab: dict[int, VocabEntry],
    paper: int,
) -> tuple[list[RunProposal], list[dict[str, Any]]]:  # (runs, drops)
```

It reuses `extract/proposals.py`'s `RunProposal`, `resolve_measurand`
and `_tier`, and `extract/verify.py`'s `literal_supported` and
`unit_has_text_support`.

- `parsed.ragged_rows` non-empty → the whole table drops,
  `ragged_table`.
- One output run per non-empty cell of each `role: "output"` series.
  Empty cells, `-`, `—`, `n/a`, `nd`, `–` alone and `skip_cells` are
  skipped. `literal` = `cell.text` (verbatim, footnote included). Parse
  with `parse_literal(cell.value_text)` and pass the parsed fields
  explicitly on the `MeasureSpec`, so the footnote does not turn the
  value categorical.
- `transform: log10` sets `value_num = 10**x` (`value_err` propagates to
  the interval `[10**(x-e), 10**(x+e)]` as `value_low`/`value_high` with
  `value_form='interval'`). It also sets
  `measurand_status='interpreted'`, `meta.transform='log10'` and
  `meta.printed_value=x`. A categorical cell under a transform drops
  `bad_shape`.
- `subject` = the subject-axis cell's `markup_surface` text;
  `subject_group` = `group`.
- Unit: the series `unit`, or the `unit_index` cell. The unit needs text
  support in the header, the unit cell or the caption, else the row
  carries flag `unit_without_text_support` (pilot rule). A unit
  `measure_units` cannot convert to the measurand's dimension drops
  that series, `unit_dimension_mismatch`, and names both units.
- `measurand_status`: `explicit` when the series label contains the
  measurand's name or an alias (case-insensitive, on `markup_surface`),
  else `interpreted`, overridable to `ambiguous`.
- Attribution: if `Column.cited` (or `row_label_cited` for
  `"rows"`) holds for the series, or for the subject cell in that
  series' row or column, then `source_attribution='cited_work'`,
  `tier='asserted'`. Otherwise the recipe values apply.
- Anchor: `[chunk_id, cell.start, cell.end]`, `anchor_scheme` the
  current `ANCHOR_SCHEME`. `literal_supported(literal, chunk_text[start:end])`
  must hold, else drop `literal_not_in_span`. It cannot fail when
  offsets are exact, so a failure is a parser bug, and the test pins it.
- `role: "input"` series become input rows of every output run in the
  same row (or column), each anchored on its own cell. `conditions`
  become input rows of every run in the table. A `caption` condition is
  anchored in the caption chunk on the literal's position there. If
  the literal is not in the caption, the condition is still written
  and flagged `anchor_mismatch` by `insert_measure`'s existing
  computation, never refused.
- An unknown measurand drops that series only (`unknown_measurand`).
- `meta.table = {"chunk": chunk_id, "table": n, "row": r, "col": c,
  "header": <series label>, "row_span": [s, e], "caption_chunk": id|null,
  "footnote": "a"|null, "orientation": "columns"|"rows"}` is set on
  every output and input row.

### 2c. `survey_extract` wiring

In `workers/job_types/survey_extract.py::_process_chunk`: a `table`
chunk where `parse_table` returns a grid with no ragged rows takes the
recipe path, as one routed call on op `survey_extract:table_recipe`
(registered in `utils/llm/operations.py` with `Tier.MEDIUM`), then
`expand_recipe`, then the same `insert_measure` write loop, dup-skip,
glyph flag and escalation handling. A table that does not parse, or
that is ragged, keeps today's per-cell prompt, so nothing regresses.

- New param `chunk_kinds: list[str] | null`. `["table"]` runs the
  tables-only pass. It defaults to `_EXTRACT_CHUNK_KINDS`, and an
  unknown kind refuses the job up front.
- The caption lookup reuses slice 1's `assign_captions` over the
  `_chunks` list the job already loads.
- `summary_meta` gains `drops_by_reason`, `flags_by_kind` and
  `tables: {recipe, fallback, no_results, ragged}` (T1's escalation
  counts).
- `max_tokens` for the recipe call: 1024. A truncated reply counts
  `truncated` and falls back to the per-cell prompt for that chunk.
- `measure_accept`'s sampler prompt (`extract/prompt.py::review_prompt`)
  gets `meta.table`'s header, row label and caption when the row has
  one. The reviewer otherwise sees one cell with no column name.

### Slice 2 acceptance

1. Sample-1 fixture (with one cell changed to `<1`) plus a recipe with
   4 output columns → 4 × n_rows output runs. `<1` lands `upper_bound`,
   and each run carries the caption's temperature as an input row
   anchored in the caption chunk.
2. Sample-2 fixture (`orientation: rows`) → the prefactor series
   writes `13` as point `value_num = 1e13` and `15 ± 2` as interval
   `[1e13, 1e17]`, both with `meta.transform='log10'`. `13.5 \pm 1.1^a` writes point 13.5,
   err 1.1, `meta.table.footnote='a'`. Every run is
   `cited_work`/`asserted` even when the recipe says `own_work`.
3. Two cells in one row with the same literal and measurand, at two
   temperatures, both land as distinct rows (cell anchors). A re-run of
   the job writes 0 new rows (dup-skip).
4. A ragged table drops whole with `ragged_table` and falls back to the
   per-cell path. A recipe naming an unknown measurand drops that series
   only. A `{"table": null}` reply writes nothing and counts
   `no_results`.
5. `summary_meta.drops_by_reason` matches the escalation list.
   `tables.recipe + tables.fallback + tables.no_results + tables.ragged`
   equals the number of table chunks read.
6. The router stub records one call per table chunk on op
   `survey_extract:table_recipe`, not one per cell.

Tests: `tests/test_extract_recipes.py` (pure, AC 1–4), additions to
`tests/test_survey_extract_job.py` (dev DB plus stubbed router via
`tests/extract_helpers.py`, AC 3–6).

## Slice 4 — run it for qu500066 (operation on prod, no code)

1. **Mint the surface-kinetics measurand taxa** under `measurand`
   (`put(kind='taxon')` plus a `specialises` link, ordinary prod writes,
   each with an SI `canonical_unit` and a `display_unit`). The list,
   checked against `search(kind='taxon')` first so nothing duplicates
   tn465832 / tn465830:
   - sticking coefficient (initial / saturation as aliases or children):
     `1`
   - desorption activation energy: `J`, display `kJ/mol`. Per-mole
     energies need the J/mol variant. Use the same handling
     `activation barrier` already uses.
   - first-order desorption pre-exponential factor: `s^-1`
   - second-order desorption pre-exponential factor: `m^2 s^-1`
   - surface rate constant (first order): `s^-1`
   - saturation coverage: `1`, display `ML`
   - turnover frequency: `s^-1`
   `required_conditions`: `temperature` on rate constant and TOF.
   Prefactors and barriers get none, because tables usually state them
   bare.
2. **Run** one `survey_extract` per table-bearing qu500066 paper
   (`search(kind='paper', args={'serves': 'quest:500066',
   'has_fulltext': True})` → 262 papers, 143 with tables), with
   `params={'paper': …, 'chunk_kinds': ['table'], 'run_token':
   'qu500066-tables-1', 'measurands': [<the slice-4 taxa + tn465832 +
   tn465830>], 'skip_accept': true}`, then one run-level
   `measure_accept` on the token (finding 1: per-paper batches cannot
   reach n=52).
3. **Report on the gripe:** rows written, drops by reason, the
   acceptance verdict. Then close gr506762 if the verdict passed, which
   clears qu500066's `needs-tool` gap. If it failed, report the failure
   classes and leave it open.

### Slice 4 acceptance

`search(kind='measure', property='<sticking taxon>',
args={'serves': 'quest:500066'})` returns ≥1 accepted row, each with a
table-cell anchor that `get(kind='measure')` resolves. The
`measure_accept` verdict is on the record. The gripe carries the numbers.

## In scope

Slices 1–4 above: `extract/tables.py`, `extract/recipes.py`, the recipe
prompt and op, the `survey_extract` table branch, `view='tables'`,
`view='measures'`, the `chunk_kind` paper-search arg, the
`paper`/`serves` measure-search args, the skill updates, and the
qu500066 run.

## Explicitly NOT in scope

- New PDF table extraction (docling, GROBID, camelot) or re-ingest.
  Tables come from the stored Marker output only.
- Repairing non-pipe or collapsed-OCR tables, inferring column
  boundaries, digitising figures, or SI/supplement tables that were
  never ingested.
- A raw cell store or any migration (decision 2).
- Prose-path changes: T2 group condition sets, T3 subject promotion,
  sampler-context widening (finding 3). Those stay in
  `corpus-quantitative-extraction.md`.
- Cross-paper identity of subjects ("Ni{100}" in two papers), and
  unit-registry additions beyond what pint parses. A Langmuir exposure
  unit (`L`, which pint reads as litre) drops `unit_dimension_mismatch`
  and is counted, not special-cased.
- Running the recipe pass corpus-wide (124k table chunks). That is a
  separate filler-lane decision once slice 4's acceptance is known.
- Changing ingest's table chunking or caption pairing. Captions are
  found when read.

## Acceptance criteria

The per-slice lists above (slice 1: 5, slice 2: 6, slice 3: 2,
slice 4: 1). Done for the item means all of them pass, and
`scripts/test tests/test_extract_tables.py tests/test_paper_tables_view.py
tests/test_extract_recipes.py tests/test_survey_extract_job.py
tests/test_measure_kind.py` is green.

## Target + blast radius

- New: `src/precis/extract/tables.py`, `src/precis/extract/recipes.py`,
  `src/precis/handlers/_paper_tables.py`, the tests named above.
- Changed: `handlers/paper.py` (`_SUPPORTED_VIEWS`, two view branches,
  the `chunk_kind` search kwarg), `handlers/_paper_search.py`
  (threading `chunk_kinds`), `store/_chunks_ops.py`
  (`table_chunks_for_ref`), `extract/prompt.py`
  (`table_recipe_prompt`, review-prompt table context),
  `extract/proposals.py` (shared table-line helpers only),
  `workers/job_types/survey_extract.py` (table branch, `chunk_kinds`
  param, summary counts), `utils/llm/operations.py` (new op),
  `store/_measures_ops.py::search_measures` (paper/serving scope),
  `handlers/measure.py` (args, table line).
- Skills: `precis-paper-help`, `precis-measure-help`.
- No migration, no ingest change, and no change to `insert_measure`'s
  guards.
- Post-deploy look: `get(kind='paper', id=<a qu500066 paper with
  tables>, view='tables')` renders. `search(kind='paper', q='sticking
  coefficient', args={'chunk_kind': 'table', 'serves': 'quest:500066'})`
  returns table hits.
- Thread: knowledge-mesh owns `corpus-quantitative-extraction.md`.
  Its slice T1 now points here.

## Open questions / decisions log

- **[decided, this spec]** Storage is `measures`, there is no
  migration, and the anchor is the cell. See decisions 2 and 5.
- **[decided, this spec]** Deterministic grid plus LLM recipe, on a new
  `Tier.MEDIUM` op inside `survey_extract`. See decisions 3 and 4.
- **[open, Reto] Lane for the qu500066 run (slice 4).** Option (a):
  cloud haiku-class on the new op. Estimate: 1,406 pipe tables × about
  4.5k input tokens plus 0.5k output ≈ $10, plus the frontier sample
  in `measure_accept` (about 60 reviews, a few dollars). It finishes
  in an hour. Option (b): the castor local chain, which is free but
  queues behind the gardener trial and has failed on placement before
  (jo496639). **Recommendation: (a)**, since it is under the $25
  threshold. Corpus-wide runs stay local.
- **[open, Reto] Prefactor taxa split by reaction order.** One canonical
  unit per measurand forces it: first-order `s^-1` and second-order
  `m^2 s^-1` cannot share a taxon. **Recommendation:** split by order as
  listed in slice 4, and treat an order the table does not state as
  `measurand_status='ambiguous'`, which `best_measure` skips.
- **[open, non-blocking] Log-scale cells.** Slice 2 stores `10**x` with
  `measurand_status='interpreted'` and the printed exponent in meta.
  The alternative is a separate `log10-prefactor` measurand, which
  keeps the literal's own scale but splits the census in two.
  **Recommendation:** the transform as specified.
