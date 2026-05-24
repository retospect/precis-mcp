---
id: precis-toon
title: precis — TOON tabular output
status: active
tier: 2
floor: any
applies-to: precis CLI piped output, MCP tabular responses
last-updated: 2026-05-22
---

# precis-toon — Token-Oriented Object Notation

`precis` emits tabular results in **TOON** (Token-Oriented Object
Notation) when stdout is piped or when the caller asks for
`--format toon` explicitly. TOON is roughly 40 % cheaper than
indented JSON for homogeneous lists because the column keys
appear once in the header instead of once per row.

## Shape

```
col1<TAB>col2<TAB>col3
val1<TAB>val2<TAB>val3
val1<TAB>val2<TAB>val3
```

- The first non-blank line is the **header** — tab-separated
  column names.
- Every subsequent line is a **row** — tab-separated cell values
  in the same order as the header.
- The document does not end with a trailing newline; agents
  should tolerate one anyway (pipes and `print` add one).

The delimiter is `\t` — paper titles routinely contain commas,
which would otherwise force quoting on every row. Other tools
in the precis ecosystem keep the same convention.

## Escape rules — minimal by design

The audience for `precis` TOON output is an LLM, not a parser.
The wrapping rules are tuned for token frugality:

- A cell is wrapped in `"…"` **only** when it contains the
  delimiter, a newline, or a carriage return — characters that
  would otherwise confuse the column / row structure.
- Bare double quotes inside cells pass through verbatim. You'll
  see `He said "hi"` as a single cell with literal quotes; the
  column boundary is the tab, not the quote.
- When a cell *does* need wrapping (because of an embedded tab
  or newline), inner `"` characters inside the wrapper are
  doubled (`""`) per RFC 4180 so the closing-wrapper boundary
  stays unambiguous.
- An empty cell renders as the empty string.

A consequence of the minimalism: a cell whose value *starts*
with a literal `"` is ambiguous to a strict parser (the leading
quote looks like a wrapper open). LLMs read it correctly from
context, and `precis` does not parse its own output in
production, so this is by design rather than an oversight.

## Parse it in Python

The standard library handles TOON as-is via `csv.DictReader`:

```python
import csv, io
text = open("/path/to/output.toon").read()
rows = list(csv.DictReader(io.StringIO(text), delimiter="\t"))
```

Or use the precis helper:

```python
from precis.format import toon
rows = toon.load(text)   # list[dict[str, str]]
```

`toon.load` returns dicts of strings — type recovery (e.g.
`int(row["pending"])`) is the caller's job; TOON is a transport,
not a schema language.

## Choose the format

- **Default on a TTY**: `table` — ASCII box-drawing for
  readability. Not meant to be parsed.
- **Default when piped**: `toon` — tab-separated, minimal token
  overhead.
- **Explicit override**: `--format {toon,json,table}`. Use
  `json` for nested or single-record output.

```sh
precis worker --status              # TTY → ASCII table
precis worker --status | cat        # pipe → TOON
precis worker --status --format json
```

## Why not JSON everywhere?

JSON wastes tokens on repeated keys in homogeneous lists. A
50-hit search response in indented JSON repeats every column
name 50 times; the same response in TOON has one header and 50
slim rows. The agent-facing token cost goes from ~3 K to ~1.8 K
on a typical search payload.

Single-record responses (e.g. `precis show <handle>`) still emit
JSON — they have no repeated structure to compress, and JSON
parsers are universal.

## See also

- `precis-overview` — verbs and kinds
- ADR 0002 (`docs/decisions/0002-pub-id-and-toon.md`) — why TOON
- `precis worker --help` — the canonical CLI surface using TOON
