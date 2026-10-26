---
status: draft
pillar: 3d-design
title: BOM tables — table views over the component assembly tree, promote scratch tables, pinned revisions
model: opus
---

# BOM tables

Depends on [`spreadsheet-tables.md`](spreadsheet-tables.md)
slices 1–4 (ids, TOON, formulas, pulls, vendor adapters). Design settled
with Reto in conversation 2026-10-26.

## Motivation / why

Reto wants BOM management: stable part/line references across many
projects, a BOM rendered in drafts with stock and price, and a way to go
from "mucking around" sourcing tables to a real BOM.

The BOM is already native: the `component` kind's assembly tree
(`put(kind='component', id=…, contains=…, qty=…, ref_designator=…)`,
cycle-checked) and `view='bom'` (flattened leaves, path-multiplied qty,
cost and mass rollups, `spec=` consistency, the consolidation nudge in
`src/precis/bom_nudge.py`). The `pcb` kind has its own netlist BOM. So
tables must not become a second BOM store: they are scratch space before a
BOM exists, and views of it afterward.

## Design

### Identity — no hierarchical BOM numbers

Hierarchical numbers (`1.2.3`) are positions and shift on every insert,
the same reason A1 was dropped. Stable ids already exist:

| need | stable id |
|---|---|
| internal part number | component handle (`cp881`), never reused |
| one BOM line | the `contains` link (parent → child), qty + designator on it |
| same part in two subassemblies | the handle path (`cp100/cp240/cp881`) |
| board positions | `ref_designator` (`R1`, `J3`) on the link |

Drawing item numbers (1, 2, 3…) are display, numbered by the view and
frozen when a BOM table is pinned.

### Projects

No project prefix. A project is its top-level todo (the existing "owning
project todo" used by drafts and plans); a product's BOM root is one
top-level component linked to that todo. "BOM for project X" = project
todo → root component → `view='bom'`.

### 1. BOM → table (`from_bom=`)

`put(kind='draft', chunk_kind='table', from_bom='component:<root>',
columns=[…])` (or `from_bom='pcb:<design>'`) makes a table whose rows are
BOM lines, keyed by link path. Built-in columns: path, component ref
(`role='subject'`, so `tabulates`), qty (path-multiplied), designator.
Further columns pull from the component (`ref:<spec>`, current) and
vendors (`vendor:<field>` via the component's stored supplier links),
plus the user's own formula columns. Tree changes appear on the next read.
Qty and membership are not editable in the table: a patch on them is
refused with a `next=` pointing at `put(kind='component', contains=…,
qty=…)`.

### 2. Table → BOM (`promote_to=`)

`edit(id='dc<id>', promote_to='component:<root>')` turns a scratch
sourcing table (vendor, part, qty, …) into components + `contains` links:
match existing components by vendor part number, create the rest, write
qty and designators, then convert the table into a `from_bom` view. One
call; the reply lists matched vs created and any row it could not place
(missing part number, ambiguous match). `dry_run=True` previews.

### 3. Revisions = pinned BOM tables

Pinning a `from_bom` table at release freezes lines, qty, prices and item
numbers with `pinned_at`. A revision is a pinned table linked to the
project todo (rel per existing vocabulary); rev A / rev B are successive
pins. No new revision mechanism.

## Explicitly NOT in scope

- A second BOM store, or implicit tree edits from a table.
- Hierarchical/positional BOM numbers as identity.
- A project kind or project-prefixed ids.
- ECO/approval workflows, cost rollups beyond what `view='bom'` and table
  formulas give.
- Changing the `pcb` BOM export (.csv) itself.

## Acceptance criteria

- `from_bom` on a 3-level assembly lists one row per leaf line with
  path-multiplied qty matching `view='bom'`; adding a `contains` edge shows
  a new row on the next read without editing the table.
- A patch on a `from_bom` table's qty is refused with the component `put`
  in `next=`.
- `promote_to` on a 10-row scratch table creates/matches components and
  links so that `view='bom'` on the root equals the table's qty column;
  the table then reads as a `from_bom` view; a row with no part number is
  reported, not dropped silently.
- Pinning a `from_bom` table and then changing the tree leaves the pinned
  table's rows and item numbers unchanged.
- `from_bom='pcb:<design>'` produces rows matching the pcb `view='bom'`.

## Target + blast radius

- `src/precis/handlers/component.py`, `src/precis/handlers/pcb.py` — a
  shared BOM-line iterator the table source reuses (the rollup logic
  behind `view='bom'`).
- Table row-source hook in the spreadsheet-table modules
  (`table_pull.py`).
- `promote_to` in `src/precis/handlers/draft.py` calling the component
  store.
- Skills: `precis-component-help` (BOM tables section), the new
  `precis-table-help`.

## Slices

1. `from_bom` for component trees (read-only lines + pulled columns).
2. `promote_to`.
3. `from_bom` for pcb designs.
4. Revisions as pinned tables linked to the project todo.

## Open questions / decisions log

Decided (Reto 2026-10-26): BOM stays native in `component`; tables are
scratch + views; no hierarchical numbers or project prefix; revisions are
pinned tables.

Open (non-blocking):
- Which existing relation links a pinned revision table to the project
  todo, or whether it needs one.
- Matching rule in `promote_to` when a vendor part number maps to two
  components.
