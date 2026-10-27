---
id: precis-table-help
family: core
title: precis — spreadsheet tables in drafts (TOON view, patch, structure ops)
summary: read and edit a draft's chunk_kind='table' chunk as an id-addressed TOON grid — view='toon', patch=, toon=, base=, structure ops
answers:
  - how do I read a draft table as TOON with row/column ids?
  - how do I patch cells in a table without resending the whole grid?
  - how do I add, drop, move or rename table rows and columns?
  - how do I avoid clobbering a concurrent table edit (base=)?
applies-to: edit(kind='draft') on a chunk_kind='table' chunk
tags: [drafting, verbs, addressing]
status: active
---

# precis-table-help — tables as id-addressed TOON grids

A `chunk_kind='table'` draft chunk holds `meta.table` =
`{header, rows, columns, rowmeta}` — every row and column carries a
stable 4-char id (never reused after delete). The TOON view is the
read/edit surface for agents; ids come from meta, never the stored text.

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
- Reply: changed-cell count, the changed/appended rows as TOON, new
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
`drop_rows=[<id>, 'a..b']` · `sort='qty desc'`.

`at=` positions inserts/moves: `'end'` (default) | `'start'` |
`{'after': '<id>'}` | `{'before': '<id>'}`. One op per edit — passing
two selectors refuses. Each reply carries the new TOON (filtered to
the touched part past 200 rows) and the new `hash:`.

## base= — don't clobber a concurrent write

Pass `base=<hash>` from the read. If the table changed since and the
change overlaps this edit's touched cells, the write refuses — re-read
(`view='toon'`) and redo. A bounded `meta.table.recent` journal makes
the overlap check work; a base older than the journal refuses
conservatively.

## See also

- [[precis-draft-rich-content-help]] — table= whole-grid replace, cell=, find=/sub=, LaTeX-sourced tables.
- [[precis-toon]] — the TOON wire format itself.
- [[precis-draft-help]] — the draft kind and verb surface.
