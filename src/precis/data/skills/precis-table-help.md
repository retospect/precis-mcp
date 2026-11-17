---
id: precis-table-help
family: core
title: precis — spreadsheet tables in drafts (TOON view, patch, structure ops)
summary: Excel, xlsx, CSV, sheet, grid, formula, calculated column, total, sum a column, bill of materials, budget — create, read and edit a draft's chunk_kind='table' chunk as an id-addressed TOON grid — view='toon', patch=, toon=, base=, structure ops
answers:
  - how do I edit an Excel file, add a column and a formula?
  - can I import or export xlsx or CSV files?
  - how do I create a budget sheet with rows, columns and totals?
  - how do I make a bill of materials table with formulas and sum a column?
  - how do I read a draft table as TOON with row/column ids?
  - how do I patch cells in a table without resending the whole grid?
  - how do I add, drop, move or rename table rows and columns?
  - how do I avoid clobbering a concurrent table edit (base=)?
applies-to: put/get/edit(kind='draft') on a chunk_kind='table' chunk
tags: [drafting, verbs, addressing]
status: active
---

# precis-table-help — tables as id-addressed grids

A `chunk_kind='table'` draft chunk holds `meta.table` =
`{header, rows, columns, rowmeta}` — every row and column carries a
stable 4-char id (never reused after delete). The toon view is the
read/edit surface for agents; ids come from meta, never the stored text.

Excel/xlsx and CSV file import and export are not supported. Tables live
inside drafts; there is no standalone `table`, `sheet` or `spreadsheet` kind.

## First table in three calls

Create the owning project todo, create its draft, then add the table:

```python
put(kind='todo', title='Budget')  # save the returned todo id
put(kind='draft', id='budget', title='Budget', project='<todo-id>')
put(kind='draft', id='budget', chunk_kind='table',
    columns=[{'name': 'item', 'key': True},
             {'name': 'qty'}, {'name': 'unit_price'},
             {'name': 'cost', 'formula': 'qty*unit_price'}],
    rows=[['Bolts', 4, 0.5], ['Brackets', 2, 3]],
    footer={'cost': 'sum'})  # save the returned dc<id> chunk handle
```

Use the returned numeric todo id for `project=`; a project title does not
create a project on this path. Reuse an existing project/draft to skip those
calls. Subsequent table reads and edits address the returned `dc<id>`.

`columns[].key=true` declares the key column: its values identify rows for
patch/upsert when no row id matches. Choose unique, non-empty values.
Formula columns calculate per row from column names; rows above omit their
calculated cells. A column total uses `footer={'cost': 'sum'}`, not
`formula='sum(cost)'`. Footer functions: `sum mean median std min max count`.
Read aggregates under the grid; `view='stats'` reports column statistics.
For the full formula grammar, error markers and footer rules see
[[precis-draft-rich-content-help]] §Formulas, footer aggregates & stats.

## view='toon' — the agent-facing grid

```python
get(kind='draft', id='dc<id>', view='toon')
# filters: get(..., view='toon', args={'cols': ['qty','k5f9..m3qa'],
#   'rows': ['r2k7','h8n3..x9aa'], 'where': 'stock < 1000'})
```

```text
{#	k5f9:vendor	m3qa:part	x7d2:qty	t6ne:line_cost =unit_price*qty}
r2k7	jlcpcb	C19077769	100	1.23
h8n3	jlcpcb	C25804	50	0.04
hash: 3e287c51a935
```

- `#` = the row-id column. Header cells are `<id>:<name>` plus optional
  ` [unit]`, ` <from>`, ` =formula` suffixes (in that order).
- `cols=`/`rows=` take ids, column names, or `a..b` ranges in current
  order. `where=` is a Python-flavoured predicate over column names —
  comparisons, `and`/`or`/`not`, `in`; a non-comparable row just fails.
- `hash:` on the last line is the content hash — feed it back as
  `base=` to guard a `patch=`/`toon=`/structure write.
- `view='table'` renders the same selection as GFM (humans/export).

## patch= — subset edit by ids

```python
edit(kind='draft', id='dc<id>',
     patch="{#\tvendor\tqty}\nr2k7\tjlcpcb\t120\n",
     base='3e287c51a935', dry_run=True)   # dry_run previews
```

- Columns resolve by `id:name`, bare id, or bare name — unknown →
  refused listing valid ones. Declared unit/from/formula must match
  the stored spec; calculated/pulled (`formula`/`from`) columns are
  refused — they re-derive.
- Rows match by row id, else by the declared `key` column's value.
  **'' = unchanged**, **`null` = clear**, other values are type-inferred
  (a `type='text'` column stays verbatim).
- `mode='update'` (default) refuses the whole patch on any unmatched
  row; `mode='upsert'` appends them (needs a `key` column — the `#`
  cell supplies its value).
- Reply: changed-cell count, the changed/appended rows in toon form, new
  `hash:`.

## toon= — full replace

`edit(..., toon='<toon>')` replaces the whole grid. Ids in the header
and `#` cells are kept (a `view='toon'` → `toon=` round-trip is
identity); an empty `#` mints. Omitted calculated/pulled columns
persist. Dropped ids retire — they never reappear.

## Structure ops — by stable id

`add_column={'name':'note','type':'text'}` · `drop_column=<id|name>` ·
`rename_column={'from':<ref>,'to':'memo'}` · `move_column=<ref>` ·
`move_row=<id>` · `rows=[[…]]` (cell lists in column order) ·
`drop_rows=[<id>, 'a..b']` · `sort='qty desc'`. (`add_column` also takes
`'formula'`/`'from'` keys — that is how a formula column is added to an
existing table; `drop_column` takes the column's footer aggregate with
it.)

`at=` positions inserts/moves: `'end'` (default) | `'start'` |
`{'after': '<id>'}` | `{'before': '<id>'}`. One op per edit — passing
two selectors refuses. Each reply carries the new grid (filtered to
the touched part past 200 rows) and the new `hash:`.

## base= — don't clobber a concurrent write

Pass `base=<hash>` from the read. If the table changed since and the
change overlaps this edit's touched cells, the write refuses — re-read
(`view='toon'`) and redo. A bounded `meta.table.recent` journal makes
the overlap check work; a base older than the journal refuses
conservatively.

## See also

- [[precis-draft-rich-content-help]] — creation, formula grammar, footer aggregates, stats, table= whole-grid replace, cell=, find=/sub=, LaTeX-sourced tables.
- [[precis-toon]] — the toon wire format itself.
- [[precis-draft-help]] — the draft kind and verb surface.
