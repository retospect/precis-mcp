---
status: draft
title: Structured enrichment for rich draft tables (alignment, spans, rules, footnotes) — format-neutral, not stored markup
model: opus
---

# Structured enrichment for rich draft tables

## Motivation / why

`chunk_kind='table'` draft chunks store canonical data as
`meta.table = {header, rows}` (JSON scalars; ADR 0035 §1) and derive
everything else — the GFM `text` projection, the LaTeX `longtable`, the
Word `add_table` — from that one structure. This is deliberate: precis
exports to **both** PDF and Word from a single canonical source, so the
store must stay **format-neutral** (the reason raw LaTeX tabular source is
rejected — the docx path cannot consume it; see
`docs/proposals/draft-table-editing.md`, "Why we do NOT store LaTeX").

The gap this proposal closes (item 3, deferred out of the field-editing
ship b9bc1d4c): the `{header, rows}` model captures a table's **data** but
none of its **presentation structure**. Real scientific tables carry:

- **column alignment** — numeric columns right/decimal-aligned, labels left;
- **merged cells** — `\multicolumn` (a header spanning several data
  columns) and `\multirow` (a row label spanning several rows);
- **extra rules** — booktabs `\cmidrule` partial rules under a grouped
  header, or a `\midrule` between row groups;
- **footnote markers** — a superscript `a`/`b` on a cell keyed to a note
  block below the table.

Today all four are lost on ingest/authoring: `normalize_table` flattens to
a plain grid, and both exporters render an unruled, all-left, unmerged
table. gr178501's structural-loss complaint is the data-loss face of this.

**The wrong fix is to store LaTeX** (breaks Word; re-flattens on the docx
reverse-parse). **The right fix is to extend the structured schema** so
*both* exporters render the richness natively — which is what this proposal
specifies.

### What the exporters do today (grounding)

Both read the same payload and emit native output — neither reads any
presentation field yet:

- **LaTeX** — `_render_table` in `src/precis/export/latex.py`: a
  `longtable` with equal-width `p{…}` columns, hard-coded
  `\toprule` / `\midrule` (after header) / `\bottomrule`, every cell
  `\raggedright`. No alignment, no spans, no extra rules.
- **Word** — `_render_table` in `src/precis/export/docx.py`: an
  `add_table` with a `Table Grid` style, bold header row, one paragraph per
  cell. No per-cell alignment, no `cell.merge()`, no custom borders.
- **Already present in both** — superscript runs (`\textsuperscript{…}` /
  `run.font.superscript`), so footnote **markers** are the cheapest rung;
  the note **block** is a new paragraph/`tablenotes` environment.

## In scope

Four **optional, additive** fields on `meta.table` (a plain table has none;
full back-compat — `normalize_table` defaults each to absent/empty and old
chunks render exactly as today). Every field is **structured data**, never
markup, and every field is read by **both** exporters.

1. **`align: [str]`** — per-column alignment, length = `len(header)`, each
   in `{'l','c','r'}` (optionally `'d'` decimal-align, deferred to an open
   question — needs `siunitx` in LaTeX and has no clean Word analogue).
   LaTeX: the `p{…}` colspec gains `\raggedleft`/`\centering`. Word:
   `paragraph.alignment` per cell. Lowest risk, highest daily value.
2. **`spans: [{row, col, rowspan?, colspan?}]`** — a sparse list of merged
   cells, addressed in the **same 1-based, header-is-row-1 coordinate
   system as `cell=`** (proposal item 1). `colspan` ≥ 2 merges rightward,
   `rowspan` ≥ 2 merges downward; the covered cells in `rows` hold `null`.
   LaTeX: `\multicolumn{colspan}{c}{…}` and `\multirow{rowspan}{*}{…}`
   (adds `\usepackage{multirow}` to the preamble). Word: `cell.merge()` /
   the vertical-merge helper on the native table. **Because spans are
   stored, no exporter reverse-parses** — the docx re-flatten problem that
   sank stored-LaTeX does not arise.
3. **`rules: [{after_row: int, cols?: [start, end]}]`** — extra horizontal
   rules **beyond** the default top/header/bottom. `after_row` is 1-based
   (header = 1); `cols` (1-based inclusive) scopes a partial rule, absent =
   full width. LaTeX: `\cmidrule(lr){start-end}` or a full `\midrule`.
   Word: bottom border on the spanned cells (see the border risk below).
4. **`notes: [{marker: str, text: str}]` + `cell_marks: [{row, col,
   marker}]`** — footnote block + the superscript markers that key into it.
   Markers stay **out of cell text** (a `cell_marks` overlay, not `"3.2^a"`
   in the cell) so the numeric value stays a clean JSON scalar for the
   numerics index. LaTeX: `\textsuperscript{marker}` appended in-cell +
   a `threeparttable`/`tablenotes` block (adds `\usepackage{threeparttable}`).
   Word: a superscript run appended in-cell + a small-font paragraph after
   the table.

Plus the **plumbing** that carries these end-to-end:

- `normalize_table` validates each new field (bounds-check against
  `header`/`rows` dimensions; reject overlapping spans; reject a `marker`
  in `cell_marks` with no matching `notes` entry) and preserves it verbatim.
- `set_cell` / `find_replace_cells` (the item-1 edit paths) leave the
  presentation fields untouched; a `table=` full-replace may set them.
- A small **edit surface** for each (e.g. `edit(kind='draft', id='dc<id>',
  align=[…])`) — or fold them under the existing `table=` replace only.
  **Open question** — see below.

## Explicitly NOT in scope

- **Storing LaTeX / any format-specific markup.** Same rejection as the
  parent proposal — it breaks Word and the one-canonical-source contract.
- **Arbitrary per-cell styling** (font, color, shading, borders on every
  edge). This proposal covers *structural* richness (alignment, spans,
  rules, footnotes), not a cell-format language. Word shading / colored
  rules are a separate, lower-value ask.
- **Nested tables, cells containing block content** (lists, multiple
  paragraphs). Cells stay single inline-grammar strings.
- **A formula/compute layer.** Values remain literal JSON scalars; a
  spreadsheet compute engine is the figure/`calc` lane (ADR 0035 §2/§3),
  not this.
- **CSV/Excel round-trip of the enrichment.** stdlib `csv` interchange (if
  built) carries data only; presentation fields are export-only.
- **A web table editor** for these fields. MCP/programmatic surface only.

## Acceptance criteria

- A table with `align=['l','r','r']` renders right-aligned numeric columns
  in **both** a LaTeX build (colspec/`\raggedleft`) and a Word export
  (`paragraph.alignment = RIGHT`) — verified by rendering and asserting the
  emitted colspec / the cell paragraph alignment.
- A `spans=[{row:1, col:2, colspan:2}]` header merge emits one
  `\multicolumn{2}{c}{…}` in LaTeX and one merged cell (`cell.merge`) in
  Word, with the covered `rows` cell (`null`) not double-rendered.
- A `cell_marks` + `notes` pair emits `\textsuperscript{a}` in-cell + a
  `tablenotes` line in LaTeX, and a superscript run + a trailing note
  paragraph in Word; the marked cell's stored value stays a clean scalar
  (no `^a` in `meta.table`), verified against the numerics path.
- A **plain** table (none of the new fields) renders byte-identically to
  today (pin with the existing table-export tests — no regression).
- `normalize_table` rejects an out-of-bounds span, an overlapping span, and
  a `cell_marks` marker with no matching note, each with a copy-ready
  `next=`.

## Target + blast radius

- `src/precis/utils/table_data.py` — `normalize_table` (validate + preserve
  the four fields), and a shared helper the exporters call to resolve a
  cell's alignment / span / mark (keep the two exporters reading one source).
- `src/precis/export/latex.py` — `_render_table`: colspec from `align`,
  `\multicolumn`/`\multirow` from `spans`, `\cmidrule` from `rules`,
  `threeparttable` from `notes`; preamble gains `multirow` + `threeparttable`.
- `src/precis/export/docx.py` — `_render_table`: per-cell `paragraph.alignment`,
  `cell.merge()`, note paragraph. **Cell borders for `rules` need
  low-level `w:tcBorders` XML** (python-docx has no first-class cell-border
  API) — the single biggest feasibility risk; may ship `rules` LaTeX-only in
  a first cut (open question).
- `src/precis/tools/core.py` + `src/precis/handlers/draft.py` — any new
  `align=`/`spans=`/… edit params (or `table=`-only, per the open question).
- `src/precis/data/skills/precis-draft-help.md` — document the enrichment
  fields + edit surface.
- **Amends ADR 0035 §1** (richer structured table schema) — the ADR update
  rides the implementing ship.

## Open questions / decisions log

- **Edit surface: dedicated params vs `table=`-only.** Either add
  `align=`/`spans=`/`rules=`/`notes=` edit params (ergonomic, but four more
  params on an already-wide `edit` surface *and* they arrive as list/dict
  args — the same client-side backslash/serialization boundary that bit
  gr178512, though these fields carry little backslash content), or require
  the full `table=` replace to set them (fewer params, must re-send the
  grid). **Leaning:** `table=` (as a JSON **string**, the backslash-safe
  channel shipped for gr178512) sets everything at once; add a dedicated
  `align=` only, since it's the highest-frequency, lowest-structure field.
  Decide when wiring.
- **Word `rules` via `w:tcBorders`.** Confirm the low-level border XML is
  robust across templates, or ship `rules` LaTeX-only first and file the
  Word half. Spike before committing the acceptance criterion.
- **Decimal alignment (`'d'`).** Needs `siunitx` `S` columns in LaTeX and
  has no clean Word analogue (fake it with a right-align + fixed decimals?).
  Deferred unless a concrete corpus need appears.
- **Ingest population.** Should the marker/table ingester *infer* alignment
  and spans from source PDFs, or are these authoring-only for now? Inference
  is a separate, larger effort (the discovery layer, not this) — this
  proposal delivers the **schema + exporters + edit surface**; auto-population
  from ingest is explicitly a follow-up.
- **Split check.** This is one coherent deliverable (schema + both
  exporters + edit surface), but the four fields are independently
  shippable — `align` alone is a valuable first cut. The `ready` gate
  should decide whether to land it as one proposal or slice by field.
