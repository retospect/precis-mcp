---
status: ready
pillar: 3d-design
title: BOM tables — table views over the component assembly tree, promote scratch tables, pinned revisions
model: opus
---

# BOM tables

Depends on [`spreadsheet-tables.md`](spreadsheet-tables.md) slices 1a–4
(ids, TOON, formulas, pulls, vendor adapters; slice 4 below also needs 5,
`pinned`). Design settled with Reto in conversation 2026-10-26;
readiness review the same day corrected the code claims.

## Motivation / why

Reto wants BOM management: stable part/line references across many
projects, a BOM rendered in drafts with stock and price, and a way to go
from "mucking around" sourcing tables to a real BOM.

The BOM is already native: the `component` kind's assembly tree
(`put(kind='component', id=…, contains=…, qty=…, ref_designator=…)`,
cycle-checked; qty on the `contains` link, the designator under link meta
key `ref`) and `view='bom'` (`ComponentHandler._flatten_bom` /
`_bom_leaves` in `src/precis/handlers/component.py`: leaves with
path-multiplied qty **summed per distinct leaf**, cost and mass rollups,
`spec=` consistency, the consolidation nudge in `src/precis/bom_nudge.py`).
The `pcb` kind's `view='bom'` is a CSV export plus preview
(`pcb_export.bom_csv`), built from `pcb_bom_components` /
`pcb_bom_availability` rows keyed by `component_id`. So tables must not
become a second BOM store: they are scratch space before a BOM exists,
and views of it afterward.

## Design

### Identity — no hierarchical BOM numbers

Hierarchical numbers (`1.2.3`) are positions and shift on every insert,
the same reason A1 was dropped. Stable ids already exist:

| need | stable id |
|---|---|
| internal part number | component handle (`cp881`), never reused |
| one BOM line | the `contains` link (parent → child), qty + designator on it |
| same part in two subassemblies | the handle path (`cp100/cp240/cp881`) |
| board positions | designators, one comma-joined string in link meta `ref` (`R1,R4`) |

Drawing item numbers (1, 2, 3…) are display, numbered depth-first in
link-creation order, frozen when a BOM table is pinned.

### Projects

No project prefix. A project is a strategic root todo carrying
`meta.workspace` (`precis-todo-tree-help`). A product's BOM root is one
component linked to that todo with `related-to`, link meta
`{'role': 'bom-root'}` (not `contains`, which is the acyclic assembly
tree; not `plan-of`/`draft-of`, which are functional). BOM tables live in
the project's draft (`draft-of` the project todo), so revisions are found
with `get(kind='draft', project=…)`; no extra revision link.

### 1. BOM → table (`from_bom=`)

`put(kind='draft', chunk_kind='table', from_bom='component:<root>',
columns=[…])` makes a table whose rows are BOM lines.

- **Walker:** slice 1 extracts a store-level walker yielding
  `(path, link, effective_qty, designator)` per line from the
  `contains` edges (`component_contains_children` in
  `src/precis/store/_component_ops.py`). `_flatten_bom` becomes a wrapper
  over it; `view='bom'`'s rendered output and the nudge stay
  byte-identical.
- **Row ids:** derived deterministically, so addresses survive reads:
  6 characters from a hash of (root handle, child handle path), in the
  spreadsheet-tables id alphabet with its letter-first rule; on a
  collision within the table the later path (sorted) takes 8 characters.
  Same path → same id across reads and machines; a new path never
  changes an existing id.
- **Built-in columns:** path, component ref (`role='subject'`, so
  `tabulates`), qty (path-multiplied), designator. Further columns pull
  from the component (`ref:<spec>`, current) and vendors
  (`vendor:<field>` via the component's `meta.supplier_links`), plus the
  user's own columns (formulas, notes). User-column values are keyed by
  row id, so they persist while the path exists; a removed path drops its
  row and its user values (reported on the next write).
- **Read-only lines:** qty and membership come from the tree. A patch on
  them is refused with a `next=` pointing at
  `put(kind='component', contains=…, qty=…)`. Tree changes appear on the
  next read.

### 2. Table → BOM (`promote_to=`)

`edit(id='dc<id>', promote_to='component:<root>')` turns a scratch
sourcing table (vendor, part, qty, …) into components + `contains` links,
then converts the table into a `from_bom` view.

- **Matching rule:** normalise the vendor through the alias map; match
  (supplier, `supplier_part_number`) case-insensitively against
  `meta.supplier_links[]`; fall back to (`mpn`, manufacturer). Slice 2
  adds this store lookup (none exists today).
  - one hit → reuse that component;
  - zero hits → create a component; write a `supplier_link` only when the
    vendor `lookup` confirms the part;
  - two or more hits → place nothing for that row, report
    `ambiguous: cpA, cpB` with a `next=` to add a `component` column.
- **Rows without a part number** are reported, not dropped silently.
- **Scratch-only columns** (notes, ad-hoc text, own formulas) stay as user
  columns on the resulting view, values carried to the new row ids;
  vendor/part/qty columns become the built-in and pulled columns.
- One call; the reply lists matched / created / ambiguous / unplaced.
  `dry_run=True` previews without writing.

### 3. `from_bom='pcb:<design>'`

Rows are keyed by `component_id` (a pcb design has no link path), row id
derived from (design handle, `component_id`) the same way. Columns:
designators, qty, LCSC part, plus pulls.

### 4. Revisions = pinned BOM tables

Pinning a `from_bom` table at release freezes lines, qty, prices and item
numbers with `pinned_at`. Rev A / rev B are successive pinned tables in
the project's draft.

## Explicitly NOT in scope

- A second BOM store, or implicit tree edits from a table.
- Hierarchical/positional BOM numbers as identity.
- A project kind or project-prefixed ids.
- ECO/approval workflows; cost rollups beyond `view='bom'` and table
  formulas.
- Changing the pcb `view='bom'` CSV export itself.

## Acceptance criteria

- The walker refactor leaves `get(kind='component', view='bom')` output
  byte-identical on the existing BOM fixtures (nudge included).
- `from_bom` on a 3-level assembly lists one row per link path; summing
  the table's qty per leaf component equals `view='bom'`'s per-leaf qty;
  adding a `contains` edge shows a new row on the next read without
  editing the table.
- Row ids of a `from_bom` table are identical across two reads and after
  an unrelated `contains` edge is added elsewhere in the tree.
- A patch on a `from_bom` table's qty is refused with the component `put`
  in `next=`; a patch on a user `notes` column lands and survives a
  re-read.
- `promote_to` on a 10-row scratch table: rows with a unique match reuse
  the component; new parts are created; a row whose part number matches
  two components is reported `ambiguous` and placed nowhere; a row with no
  part number is reported; afterward `view='bom'` on the root equals the
  table's qty per leaf, and the `notes` column carried over.
- Pinning a `from_bom` table and then changing the tree leaves the pinned
  table's rows and item numbers unchanged.
- `from_bom='pcb:<design>'` produces the same designator set, qty and LCSC
  part per line as `pcb_export.bom_csv` for that design.

## Target + blast radius

- `src/precis/store/_component_ops.py` — the line walker;
  supplier-part-number lookup over `meta.supplier_links`.
- `src/precis/handlers/component.py` — `_flatten_bom` on top of the
  walker (no output change).
- `src/precis/handlers/pcb.py` / pcb store — row source from
  `pcb_bom_components`.
- Spreadsheet-table modules — `from_bom` row source in `table_pull.py`;
  derived row ids.
- `src/precis/handlers/draft.py` — `promote_to`.
- Skills: `precis-component-help` (BOM tables section),
  `precis-table-help`.

## Slices

1. Walker extraction (byte-identical `view='bom'`), then `from_bom` for
   component trees with derived row ids.
2. Supplier-part-number lookup + `promote_to`.
3. `from_bom` for pcb designs.
4. Revisions as pinned tables (needs spreadsheet-tables slice 5).

## Open questions / decisions log

Decided (Reto 2026-10-26): BOM stays native in `component`; tables are
scratch + views; no hierarchical numbers or project prefix; revisions are
pinned tables.

Decided at readiness review (2026-10-26, from code checks): derived
6/8-character row ids; table qty matches `view='bom'` as per-leaf sums;
pcb rows keyed by `component_id`; project = strategic root todo, root
component linked `related-to` with `role='bom-root'`; revisions found via
the project's draft, no extra link; promote matching rule above; scratch
columns carried over as user columns.

Open: none.
