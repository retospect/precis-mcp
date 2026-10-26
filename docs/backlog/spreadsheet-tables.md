---
status: in-progress
claimed-by: ss-1a
pillar: memory-graph
title: Spreadsheet tables — stable row/col ids, TOON patch, formulas, pulls from refs/tables/vendors, pin, charts
model: opus
---

# Spreadsheet tables in drafts

Design settled with Reto in conversation 2026-10-26. Follow-on for BOMs:
[`bom-tables.md`](bom-tables.md) (depends on slices 1a–5 here).

## Motivation / why

Reto wants spreadsheet facilities in precis: a table with computed fields,
columns that point at any precis ref and pull data from it, vendor stock and
price pulled by part number, TOON as the read/edit surface, Postgres
storage, rendering in drafts, and graphs. The use: pull data into a table,
write text around it, keep the data stable enough to publish.

### What exists (checked 2026-10-26)

- **Storage and rendering work.** A `chunk_kind='table'` draft chunk holds
  `meta.table = {header, rows}` (JSON scalars) in `chunks.meta`; `text` is
  a derived GFM projection, regenerated on write and numerics-indexed. The
  LaTeX and docx exporters (`_render_table`) and the web smartdraft
  (`table_payload`) render it; cells pass through the inline grammar, so a
  cell `[GaN](pa123)` already renders as a link (no `links` row is written).
- **Editing is cell-level only.** `table=` (whole grid), `cell='B3'` +
  `text=` (A1 via `parse_cell_address`/`set_cell` in
  `src/precis/utils/table_data.py`), `find=`, `sub=`. No row/column
  insert, delete or sort.
- **Graphs work.** A figure with `render=` (sandboxed Python,
  `src/precis/render/sandbox.py`) and `plots=[dc<id>]` re-renders lazily
  when a plotted table's content changes; export forces freshness.
- **Vendor lookups exist outside tables.** `src/precis/supply/` has the
  `Adapter` port (Digi-Key, Farnell, Mouser; keyword `search` only,
  returns `StockQuote`); `src/precis/pcb/jlc_api.py` does live JLCPCB
  lookups by C-number but is not an `Adapter`.
- **No formulas, no ids, no TOON** for tables. Usage is low: 68 authored
  canonical tables in prod vs ~153k ingested paper-table chunks.

### Rulings this reverses or refines

- ADR 0035 §4 (deleted in 2d54543e6; survives in the
  `0037_plots_relation.sql` comment) made `plots` the only reactive edge
  and said "not a spreadsheet". This spec keeps push-style recompute out
  (no queue, no cascade of writes) but allows **read-time pulls** across
  tables, as a DAG (see §5).
- `draft-table-structured-enrichment.md` lists a formula layer as out of
  scope; this item is that layer. Its per-column `align` should live on the
  `columns` schema below.

## Design

### 1. One table type, kept simple

A table holds its own values, calculated columns over its own columns,
and footer aggregates. Anything larger is several tables joined by pulls.
A plain table (no formulas, no pulls) is the same type with nothing
declared; there is no separate "dumb" table.

### 2. Stable row and column ids; no A1

- Every row and every column gets a **random 4-character id**, lowercase
  Crockford base-32 (`0-9a-z` minus `i l o u`), minted at creation,
  checked unique within the table (rows and columns share one pool, so an
  id resolves alone), redrawn on collision, **never reused** after delete.
- **The first character is a letter other than `p`.** The existing
  selector grammar already owns `~N`, `~N..M` (digits) and `~pN`, so an
  id like `4821` or `p123` would be ambiguous. Ids match
  `[a-hjkmnq-tv-z][0-9a-hjkmnp-tv-z]{3,7}`: 4 characters when minted,
  6–8 when derived deterministically (BOM rows in `bom-tables.md`, paper
  tables later).
- **Order is separate from identity**, as for draft chunks: each row and
  column carries a fractional `pos` key (`precis.utils.fractional`,
  `key_between`). Insert-between mints a key; sort rewrites row keys only.
- **Addresses:** `dc<id>~<row>.<col>` (cell), `dc<id>~<row>`,
  `dc<id>~<col>`. Links, pulls and backlinks store this form; it survives
  sort, insert, move and rename. A deleted id reports as dangling.
- **Separator is `.`, not `:`.** Checked 2026-10-26: a bare handle with a
  selector does not parse today. `handle_registry.parse` takes only
  `dc678`, and `parse_link_target` (`src/precis/utils/link_target.py`) then
  splits on the first `:`, so `dc678~r2k7:k5f9` would read as kind
  `dc678~r2k7`. Inline mentions: bare bracketed handles go through
  `BARE_BRACKET_REF_PATTERN` (`\[[a-z]{2}\d+…\]`, no selector group);
  `REF_PATTERN` (`kind:id~…`) accepts only `~N`, `~N..M`, `~pN`. Slice 1a
  extends `handle_registry.parse`, `parse_link_target` and
  `BARE_BRACKET_REF_PATTERN` (a new optional `sel` group) to accept
  `<handle>~<sel>` with `sel = id | id.id | id..id`; `.` keeps clear of
  the `kind:` separator. `[dc678~r2k7.k5f9]` in prose renders as a link to
  the table chunk with the cell named (web chip, LaTeX `\hyperref` to the
  table label, docx plain text).
- **Ranges are read-time selections only.** `~r2k7..as12` (rows between,
  in current order) and `~k5f9..m3qa` (columns) work in `get`/views,
  `drop_rows=` and exports. They are refused in stored references (links,
  pulls, formulas), because a range's membership changes when rows move.
  `..` matches the existing chunk-range syntax `~N..M`.
- **No list syntax inside an address.** Lists go in parameters
  (`rows=[…]`, `cols=[…]`); several cited cells are several addresses.
- **A1 is dropped entirely**: no parameter accepts a row/column number or
  `B6`. Position is output only (views print rows in order;
  `view='table'` may number them for humans). One addressing story in the
  skills.
- `cell='B3'` is removed; `cell='r2k7.k5f9'` replaces it.
  `infer_scalar` (typing on entry) stays.

### 3. Column schema

`meta.table.columns = [{id, pos, name, type?, unit?, formula?, from?,
role?, key?, align?}]`. `name` is unique per table, so callers may name a
column by id or name. `type` ∈ `number | text | bool | ref`, inferred when
absent. `unit` is display (header, chart axes) in v1. `key: true` marks a
unique key column (optional). `header` remains as a derived name list for
back-compat readers.

### 4. Formulas and aggregates (intra-table)

- **Calculated column:** `formula='unit_price*qty'`, evaluated per row
  over column names. Stored internally with column ids, displayed with
  names, so renames rewrite nothing.
- **Footer aggregates:** `footer={'line_cost': 'sum'}`; functions `sum
  mean median std min max count`. A footer reports how many rows it
  skipped as empty.
- **Engine: `simpleeval`** (Reto 2026-10-26), for per-row expressions:
  names bound to the row's column values only; operators arithmetic +
  comparison + boolean; function whitelist (`abs round min max sqrt log
  exp if_`); attribute access, subscripts, comprehensions and string
  methods disabled (simpleeval has no switch for attribute access: our
  `SimpleEval` subclass removes the `ast.Attribute` and
  `ast.Subscript` node handlers); its power and string-length caps on. Aggregates
  (footer, `view='stats'`) are our own numpy calls, not expressions.
  Formula text can arrive from an agent relaying ingested content, so the
  whitelist is the security boundary: a test feeds known simpleeval
  escape payloads and asserts refusal. Caps on cell count and wall-clock.
- **Evaluation:** on every write, in dependency order within the table;
  evaluated values are stored in the rows. (Pulled columns are resolved at
  read time instead — see §5 "Where pulls resolve".) Errors (`#DIV/0`, cycle,
  `#REF`) are stored as the cell value and named in the reply; they don't
  refuse the edit.
- **`view='stats'`:** per-column n, mean, std, range, empties; computed on
  read, never stored.
- Fits, joins, pivots are not table features; if needed, a later job
  writes a new table.

### 5. Pulls — `from=`

A column with `from=` fills from somewhere else. Three sources:

| `from=` | source | freshness |
|---|---|---|
| `ref:<field>` | the ref named in the row's `ref` column (`title`, `meta.band_gap_eV`, a component spec) | current: read at view time |
| `table:dc<id>~<col>` matched on a key | another table's column | current: read at view time |
| `vendor:<field>` | vendor adapter, from the row's vendor + part columns (or a component's supplier links) | stored, refreshed on request (§6) |

**Internal pulls form a DAG of any depth.**

- **One `pulls-from` link per (table chunk, source table chunk)**, not per
  column: `links_endpoints_relation_idx` is unique on (src ref, src chunk,
  dst ref, dst chunk, relation). The link's `meta.columns` lists the
  column pairs (`[{col, source_col}]`). The cycle check runs at column
  level over those lists (one recursive query), so A pulling `x` from B
  while B pulls `y` from A is allowed; a real column loop is refused and
  the error names it.
- **No `table:` pull on the same table** (`links_check` forbids a
  self-link, and it is a formula anyway): refused with a `next=` showing
  the equivalent `formula=`.
- `ref:` pulls write no `pulls-from` link; the `ref` column's
  `tabulates`/`cites` link (§8) already connects the two.
- Reading evaluates only the needed columns up the DAG, each source table
  read once per request, depth cap 8. Pinned tables and stored vendor
  values are leaves.
- Deleting a column that others pull from is refused, listing dependents;
  with `force=True` they show `#REF`.
- **Where pulls resolve.** Today `table_payload`, both `_render_table`s and
  the figure render read `meta.table` only. Slice 3a adds
  `resolve_table(chunk, store) -> table` (in `table_pull.py`): stored
  values + internal pulls resolved + formulas that depend on pulled
  columns re-evaluated. Every consumer goes through it: `get` views, the
  LaTeX and docx exporters (via `table_payload`), the web smartdraft, and
  the figure render's `data["tables"]`. A table with no pulls returns
  `meta.table` unchanged (no store reads).
- **The derived `text` and the numerics index hold write-time values.**
  `text` is regenerated on each write of *this* table with pulls resolved
  at that moment; it is search text, not the display. Display and export
  always use `resolve_table`.
- **Chart freshness must follow the DAG:** a figure's render key includes
  the content hashes of every upstream table, not only the plotted one;
  otherwise a chart misses an upstream change.

### 6. Vendor pulls

- **Adapter:** add exact lookup `lookup(sku) -> PartRecord | None` to the
  `Adapter` protocol (`manufacturer, mpn, description, package,
  datasheet_url, lifecycle, stock, price_breaks[(qty, price)], currency,
  retrieved`) and `may_store: bool` per vendor. Wrap `jlc_api.live_lookup`
  as the `jlcpcb` adapter: `live_lookup` needs a `Store` (it upserts into
  `parts`), so `adapters(store=None)` gains an optional store argument,
  passed by the table refresh path, and the adapter maps the
  `normalize_api_row` dict (`mfr` → manufacturer, `mfr_part` → mpn,
  `description`, `package`, `datasheet_url`, `stock`, `price` → price
  breaks; `basic` kept as an extra) onto `PartRecord`; it reports
  `configured()` as missing when no store or no JLCPCB credentials. An alias map resolves what users type
  (`LCSC.com`, `lcsc`, `jlcpcb` → `jlcpcb`). A new vendor is one adapter
  class plus an alias line.
- **Licence ruling (Reto 2026-10-26, option 3):** identity fields
  (`manufacturer`, `mpn`, `description`, `package`, `datasheet_url`,
  `lifecycle`) may be stored from any vendor. Stock and price are stored
  **only** where `may_store` (JLCPCB). Mouser/Farnell/Digi-Key stock and
  price are read-time only: fetched on `view(..., live=True)`, web
  on-demand load or export, never written. Printing them with "retrieved
  <UTC> from <vendor>" is display, not storage. Existing rulings in the
  `supply` package docstring stand (TME, RS, Nexar declined 2026-10-05).
- **Refresh:** `edit(id='dc<id>', refresh=True)` re-pulls every storable
  vendor cell for the whole table in one pass, bounded concurrency per
  vendor, one fetch per distinct (vendor, sku) per request.
  `meta.table.refreshed = {jlcpcb: '<UTC>', …}` drives a generated caption
  line ("Stock and prices as of 15 Jul 2026 (JLCPCB)").
- **Skip, don't fail:** a vendor down or unconfigured leaves its cells and
  dates unchanged and is named in the reply; a missing or out-of-stock
  part writes empty/0 with its date. Read-time-only cells show `—` with a
  per-row reason.
- `price@qty` picks the price break for the row's `qty` column.

### 7. Table modes

`meta.table.mode` ∈ `current` (default: internal pulls live, vendor values
stored with refresh on request) | `pinned` (every value, internal pulls
included, frozen at pin time with `pinned_at`; unpin restores `current`).
Pinning is how a draft keeps its numbers stable for publication.

### 8. Links: `tabulates` and `cites`

A `ref` column declares `role`:

- `role='subject'` (default) — the row is about the ref (`cp881`,
  `ma45`): writes a new `tabulates` link. Not a citation; a ref's page can
  list "tables this appears in" with one rel.
- `role='source'` — the value came from the ref (`pa123`): writes `cites`,
  so it reaches the bibliography and citation review.

**One link per (table chunk, target ref, relation)** — the unique index
`links_endpoints_relation_idx` allows no more. Its `meta.cells` is the
list of cell addresses (`['r2k7.k5f9', 'h8n3.k5f9']`) that name the
target. Every write syncs the lists: a row delete removes only its own
entries, and the link is deleted when its list empties. The same paper
cited in two cells is one `cites` link with two cells.

New relations (migration, plus the `Relation` literal in
`src/precis/store/types.py` and `precis-relations`): `tabulates` (inverse
`tabulated-in`) and `pulls-from` (inverse `pulled-by`).

### 9. TOON read, patch and replace

```text
{#	k5f9:vendor	m3qa:part	x7d2:qty	q0wc:stock <vendor:stock>	h2r8:unit_price [$] <vendor:price@qty>	t6ne:line_cost =unit_price*qty}
r2k7	jlcpcb	C19077769	100	48210	0.0123	1.23
h8n3	jlcpcb	C25804	50	912004	0.0008	0.04
```

- `get(id='dc<id>', view='toon')` — default for agents. `#` column holds
  row ids; a header cell is `<id>:<name>` plus optional `[unit]`,
  `<from>`, `=formula`. Filters: `cols=[...]`, `rows=[...]`,
  `where='stock < 1000'`. `view='table'` (GFM, humans and export),
  `view='formulas'`, `view='stats'`. Every read returns a short content
  hash.
- **Header grammar is new.** `precis.format.toon` handles the braced
  header and tab rows (`load` returns dicts keyed by header text);
  `table_toon.py` parses each header cell (`<id>:<name>`, `[unit]`,
  `<from>`, `=formula`; a bare name is allowed in patches) and the `#`
  column on top of that. Values with tabs/newlines use the existing TOON
  quoting.
- **Patch:** `edit(id='dc<id>', patch='<toon>')` — any subset of columns
  and rows. Columns match by id or name (unknown → refused, valid names
  listed). Rows match by row id, or by the key column if declared. Empty
  cell = unchanged; `null` = clear. `mode='update'` (default) refuses the
  whole patch on any unmatched row; `mode='upsert'` appends unmatched rows
  (needs a key column). Calculated/pulled columns in a patch are refused.
  `base=<hash>` refuses on a concurrent change unless the touched cells
  don't overlap. `dry_run=True` previews. Reply: changed-cell count,
  changed rows as TOON, recomputed values that moved.
- **Replace:** `edit(id='dc<id>', toon='<toon>')`, full replace.
  Calculated/pulled columns may be omitted.
- **Structure ops:** `add_column={name, type?, unit?, formula?, from?}`,
  `drop_column=`, `rename_column=`, `move_column=`, `rows=[[…]]` +
  `at='end'|{'after': '<row id>'}`, `drop_rows=[…]`, `sort='qty desc'`.
  Each replies with the new TOON (filtered to the touched part for large
  tables).

### 10. One-call assembly

`put(kind='draft', chunk_kind='table', columns=[…], footer={…},
rows=[[…]])` creates the table, pulls every row, evaluates formulas and
footer, and replies with the TOON and refresh dates. `columns_from='dc<id>'`
copies another table's column specs (no rows), so any table serves as a
template. `add_column` with `from=` fills every row in the same call.

**Rows from a pile of ids** (`rows_from=`):

- `rows_from=['pa123', 'ma45', 'cp881', …]` — one row per ref, with a
  `ref` column (`role='subject'`); further columns are `ref:<field>`
  pulls. Mixed kinds are allowed; a field a kind lacks is empty.
  "Show me these 40 things side by side" in one call.
- `rows_from=['dc678~r2k7', 'dc555~h8n3', …]` — rows gathered from other
  tables. Columns pull by name from each source row (`table:` pull, keyed
  on the source row id); it reads current values and sits in the pull DAG
  like any other pull.
- Ids are fixed at creation (a snapshot of the list, not a saved search).
  `patch`/`rows=` add more; `drop_rows=` removes.

### 11. Charts

`chart={'type': 'line'|'scatter'|'bar', 'x': '<col>', 'y': ['<col>'],
'log_y': False}` on a figure chunk, compiled by trusted code into the
existing render job; `plots` set from the source table. Axis labels come
from column names and units. `render=` stays for anything else.

### 12. Large tables

Above ~200 rows the derived `text` (embedded, searchable) becomes caption +
column specs + `view='stats'` summary instead of every row, so a patch
doesn't re-embed thousands of rows. Rows stay in `meta.table` (3000 × 12
is a few hundred KB). Moving rows to a `table_rows` Postgres table keyed
by (chunk, row id) is deferred; the patch API is designed so that move
doesn't change it. Trigger: tables past ~20k rows or concurrent patch
contention.

### 13. Migration of existing tables

- The 68 authored canonical tables get ids in a one-time **Python** pass
  (ids are random, so not SQL): a script under `scripts/` that walks
  `chunk_kind='table'` chunks with `meta ? 'table'`, skips any that
  already have `columns`, and writes ids + `pos` keys through the store's
  normal chunk-replace path (DELETE + INSERT). Idempotent, `--dry-run`
  first; runs before any link can exist. A table the pass missed gets ids
  on its first write.
- LaTeX-sourced draft tables with no stored grid convert to a stored grid
  with ids on their first structural or cell edit (Reto 2026-10-26); raw
  LaTeX layout is lost, as structured-enrichment restores spans/rules as
  data. `find=`/`sub=` keep working until then.
- Paper tables (ingested, read-only) are **not** migrated. The address
  form is reserved for them: later, position-derived ids (hash of paper,
  table, row, col) so re-extraction of the same grid keeps ids. Build when
  a consumer (citing one value in a finding) exists.

## Explicitly NOT in scope

- A1 addressing, Excel formula syntax, cross-cell formulas inside a table
  beyond column formulas and footers.
- Push-style recompute (writes cascading to other tables); jobs for fits,
  joins, pivots.
- Storing Mouser/Farnell/Digi-Key stock or price (licence).
- xlsx/CSV import and export; a web grid editor (web stays read-only).
- Unit-aware arithmetic (pint) — later slice.
- BOM features — [`bom-tables.md`](bom-tables.md).

## Acceptance criteria

- Ids: inserting, sorting and moving rows/columns never changes an id;
  a link to `dc<id>~r.c` still resolves to the same cell afterward; a
  deleted id never reappears.
- Minted ids always start with a letter other than `p` (property test
  over 10k mints); `dc<id>~4821` and `dc<id>~p12` still parse as the
  existing chunk selectors; `get(id='dc<id>~r2k7.k5f9')` returns the one
  cell; `[dc<id>~r2k7.k5f9]` in prose renders as a link in web, LaTeX and
  docx.
- No tool parameter accepts A1 or a positional row/column number;
  `cell='B3'` is refused with a `next=` showing the id form.
- TOON: `view='toon'` → `edit(toon=…)` round-trips a table unchanged
  (values, formulas, pulls, units, types).
- Patch: a 3-column × 5-row patch on a 3000-row table changes exactly
  those cells; empty vs `null` behave as specified; an unmatched row
  refuses the whole patch in `update` mode; a stale `base=` with
  overlapping cells refuses.
- Formulas: editing `qty` recomputes `line_cost` and the footer sum in the
  same edit; a cycle stores an error and the edit lands.
- Pulls: a `ref:title` column shows the ref's current title on read after
  the ref is renamed; a 3-level table chain resolves; defining a cyclic
  pull is refused naming the loop; dropping a pulled-from column is
  refused listing dependents.
- Pull resolution: a LaTeX and a docx export of a table with a `ref:`
  pull show the ref's current value (not the write-time value in `text`);
  a figure plotting that table receives resolved values; a `table:` pull
  on its own table is refused with the `formula=` equivalent; two pulled
  columns from one source table produce one `pulls-from` link whose
  `meta.columns` lists both.
- Formula security: known simpleeval escape payloads (attribute access,
  subscripts, `__class__`, format strings, huge powers) are refused.
- Chart: a chart over table A re-renders after a change in table C that A
  pulls from via B. `chart=` renders each of `line`, `scatter`, `bar` in
  both LaTeX and docx export.
- Vendor: `refresh=True` on a JLCPCB table stores stock/price with the
  refresh date; with JLCPCB unconfigured the refresh lands, keeps old
  values and names the reason; Mouser stock is never written to
  `meta.table` (assert on the stored row) but shows on `live=True` and in
  export with the retrieval line.
- Links: a `subject` ref column writes `tabulates`, a `source` column
  writes `cites`; one paper in two cells is one `cites` link with two
  `meta.cells`; deleting one of those rows leaves the link with one cell,
  deleting both removes it; the paper's backlinks show the cell
  addresses.
- Pinned: after `pin`, a source change does not alter the table's values
  on read; unpin restores current values.
- A table without `columns`/formulas/pulls renders byte-identically to
  today in LaTeX and docx.

## Target + blast radius

- `src/precis/utils/table_data.py` — schema, ids, fractional order,
  normalisation; drop A1 helpers. New `table_formula.py` (evaluator),
  `table_pull.py` (DAG resolve, cycle check), `table_toon.py` (view/patch).
- `src/precis/supply/base.py` — `lookup`, `PartRecord`, `may_store`,
  alias map; new `src/precis/supply/jlcpcb.py` wrapping `jlc_api`.
- `src/precis/handlers/draft.py`, `src/precis/store/_draft_ops.py`,
  `src/precis/tools/core.py` — new params (`patch=`, `toon=`, structure
  ops, `refresh=`, `pin=`, `columns=`, `footer=`, `rows_from=`,
  `chart=`) and views.
- `src/precis/utils/link_target.py`, `src/precis/utils/mentions.py`,
  `handle_registry.parse` — bare-handle selectors `~id`, `~id.id`,
  `~id..id`.
- `pyproject.toml` / `uv.lock` — `simpleeval`.
- `src/precis/render/figure.py` — `chart=` compiler; render key over
  upstream hashes.
- Migrations: `tabulates`/`tabulated-in` and `pulls-from`/`pulled-by`
  relations; `Relation` literal in `src/precis/store/types.py`.
- `scripts/` — one-time id backfill (Python, idempotent, `--dry-run`).
- `src/precis/supply/base.py` `adapters()` gains `store=`.
- Skills: `precis-draft-rich-content-help`, `precis-draft-reference-help`,
  `precis-relations`, `precis-toon`; new `precis-table-help`.
- Exporters: caption line for refresh dates; read-time vendor fetch at
  export.

## Slices

Each slice ships alone, in order; later slices depend on earlier ones.

- **1a.** Column schema, ids (letter-first rule), fractional order, the
  Python id backfill; address grammar in `handle_registry.parse`,
  `parse_link_target`, `BARE_BRACKET_REF_PATTERN`; `cell=` moves to ids
  (both the A1 string and the `{'row','col'}` dict forms of
  `parse_cell_address`/`set_cell` go); A1 removal in skills.
- **1b.** TOON view (filters, content hash), header grammar,
  `patch=`/`toon=`/`base=`/`dry_run`, structure ops. Needs 1a.
- **2.** Formulas (simpleeval subclass + escape tests), footer
  aggregates, `view='stats'`.
- **3a.** `ref` columns with `tabulates`/`cites` (migration for both new
  relations), `meta.cells` link sync; internal pulls (`ref:`, `table:`),
  `pulls-from` with column-level cycle check; `resolve_table` wired into
  views, exporters and figure render; chart render key over upstream
  hashes.
- **3b.** `rows_from=`. Needs 3a.
- **4.** Vendor `lookup` + `PartRecord` + `may_store` + JLCPCB adapter;
  `vendor:` pulls; refresh and dates; read-time-only columns for no-store
  vendors.
- **5.** `pinned` mode. Needs 3a and 4.
- **6.** `chart=` (line, scatter, bar); large-table text summary.

## Open questions / decisions log

Decided (Reto 2026-10-26): one table type; random 4-char ids, no A1;
named-column formulas + footers, no Excel engine; internal pulls any depth
as a DAG; vendor licence option 3 + JLCPCB stock/price stored; table-level
`current`/`pinned`; `tabulates` + `cites` by column role; LaTeX tables
convert on first edit; paper tables reserve the address form;
`simpleeval` with a whitelist; cell separator `.` (`:` collides with the
`kind:` parse, checked in code); ranges read-time only; no lists inside
addresses; `rows_from=` for refs and other tables' rows; large-table text
summary above 200 rows.

Decided at readiness review (2026-10-26, from code checks): ids start
with a letter other than `p` (selector collision); one link per
(chunk, target, relation) with `meta.cells` (unique index); no same-table
`table:` pull (self-link check); pulls resolve through `resolve_table` for
every display/export/render consumer, `text` keeps write-time values;
slice 1 and 3 split.

Open (non-blocking):
- The 200-row threshold — tune on real tables.
