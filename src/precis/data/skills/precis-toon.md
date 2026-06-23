---
id: precis-toon
title: precis — TOON tabular output format
summary: tabular output format — header in braces, tab-separated rows, used across search and TOC views
applies-to: tabular responses from search, get(view='toc'), and list views
status: active
---

# precis-toon — Token-Oriented Object Notation

Tabular `precis` responses (search hits, TOCs, list views) come back
as TOON: a header line wrapped in `{…}` followed by tab-separated
data rows. Column keys appear once, in the header.

## Read the shape of a TOON table
## What does TOON output look like?
## How is a tabular precis response structured?

```text
{handle	chunk_keywords}
pc14	z-scheme, heterojunction, photocatalysis
pc38	cocatalyst, oxygen evolution, water splitting
pc207	noxrr, copper, faradaic efficiency
```

- First non-blank line is the header: column names wrapped in literal
  `{` and `}`, tab-separated. The braces are markers so the table is
  locatable inside a larger response (preamble + table + `Next:`
  trailer). They are not part of the column names.
- Each later line is a row: tab-separated cells in header order.
- No trailing newline; tolerate one anyway.

## Search hit shape

```text
{handle	chunk_keywords}
pc22	mof, linker design, photocatalytic
pc14	z-scheme, heterojunction, photocatalysis
```

One row per hit. The `handle` column now carries the universal handle
(`pc<chunk_id>`, e.g. `pc14`) — paste it straight into `get(id='<handle>')`
(the `pc` prefix infers the kind). Order is the relevance signal.

## TOC shape

```text
# wang2020state TOC — 187 chunks, 9 clusters

{handle	keywords}
wang2020state~0..14	introduction, photocatalysis, motivation
wang2020state~15..38	z-scheme, heterojunction, band alignment
wang2020state~39..62	cocatalyst, oxygen evolution
…
```

One row per cluster. `handle` is a range (`slug~A..B`) you can pass
to `get` to read or to `get(..., view='toc')` to drill further.

## Cell-escape rules — when quoting kicks in

A cell is wrapped in `"…"` only when it contains a tab, a newline,
or a carriage return. Bare `"` characters pass through verbatim — a
cell like `He said "hi"` is a single unquoted cell. When a cell does
get wrapped, inner `"` is doubled (`""`).

Empty cells render as the empty string. `None` → empty;
`True`/`False` → `true`/`false`.

## Parse a TOON response in Python
## Decode a TOON table programmatically

Stdlib only:

```python
import csv, io
rows = list(csv.DictReader(io.StringIO(text), delimiter="\t"))
# Header still carries the {…} markers — strip if needed:
if rows:
    first_key = next(iter(rows[0]))
    rows[0] = {first_key.lstrip("{"): v for k, v in rows[0].items()}
```

Or the precis helper, which strips the brace markers for you:

```python
from precis.format import toon
rows = toon.load(text)   # list[dict[str, str]]
```

Cells always come back as `str`. Type recovery is the caller's job.

## Pick a format on the CLI

```sh
precis worker --status                  # TTY → ASCII table
precis worker --status | cat            # pipe → TOON
precis worker --status --format json    # nested/single-record output
```

`--format {toon,json,table}`. JSON is the right pick for single
records or nested structures; TOON is for homogeneous row lists.

## See also

```python
get(kind='skill', id='precis-overview')        # verbs and kinds
get(kind='skill', id='precis-search-help')     # search response shape
get(kind='skill', id='precis-toc-help')        # TOC machinery + drill-in
```
