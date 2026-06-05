# B10 — TOON output module

- **Status**: shipped 2026-05-24 (first landed in commit ab5ab20; module lives at `src/precis/format/toon.py` with tests under `tests/format/`).
- **ADR**: [`0002-pub-id-and-toon.md`](../decisions/0002-pub-id-and-toon.md) — TOON portion in force.
- **Parent plan**: [`pip-merge.md`](pip-merge.md) §B10.
- **Touches**: `src/precis/format/`, `src/precis/cli/_common.py`,
  `src/precis/cli/worker.py`, `src/precis/data/skills/`,
  `tests/format/`, `tests/test_worker_cli.py`.

## Context

ADR 0002 picked **TOON** (Token-Oriented Object Notation,
https://toonformat.dev/) for tabular agent-facing output and JSON for
nested / single-record responses. The motivation is token cost: a
list of 50 search hits in pretty-printed JSON spends ~40 % of its
tokens on repeated keys. TOON moves those keys into a single header
row, giving roughly the same payload at ~60 % of the token count.

The pip-merge plan reserves step B10 for shipping the
`precis.format` module and wiring it into the surfaces that emit
tabular data. The decision part is settled; this artefact is the
implementation spec.

## Scope (and non-scope)

**In scope**

1. A pure-Python `precis.format.toon` module with `dump()` / `load()`
   that handles the flat homogeneous-rows form (header line + tab-
   separated value rows).
2. A `precis.format.SERIALIZERS` registry so the choice of format
   is one dict lookup, not a chain of `if` branches at every
   callsite.
3. A shared CLI helper (`precis.cli._common`) that adds a `--format`
   flag and resolves the effective format from the flag, `isatty()`,
   and the registry.
4. Wire `precis worker --status` through the helper so we have a
   visible, testable demonstration of the registry + `--format`
   contract.
5. A user-facing skill `precis-toon.md` that teaches downstream
   agents how to parse the tab-separated output.
6. Tests: dump/load roundtrip, schema-pinning, escape semantics,
   registry dispatch, `--format` resolution, `worker --status` per
   format.

**Out of scope** (deferred — `OPEN-ITEMS.md` entry on B10 follow-up)

- Refactoring every `_render_list*` / `_render_search*` handler to
  return rows instead of preformatted text. Handlers today build
  text bodies directly; converting them to a row-based intermediate
  is a separate large change. B10 lands the *library* and one
  demonstrable consumer; the rest of the surface can adopt
  incrementally without further ADR work.
- A TOON output for `precis add` / `precis watch` — both emit a
  single record or a streaming event log, neither of which benefits
  from columnar formatting.
- Wiring TOON into the MCP `search` / `get` tools' response bodies.
  Same reason as the handler refactor: it requires upstream changes
  in every handler. We add the library so that work can land
  per-handler later, but the MCP surface stays preformatted-text
  for v7.0.0.

## Format spec — what `dump()` produces

A TOON document for a homogeneous list of rows looks like:

```
<col1><TAB><col2><TAB><col3>
<v11><TAB><v12><TAB><v13>
<v21><TAB><v22><TAB><v23>
```

with these properties:

- **Header row** lists the columns. Order comes from `schema=` if
  provided; otherwise it is the union of keys across all rows in
  first-seen order.
- **Delimiter** is the horizontal tab (`\t`) by default. ADR 0002
  picked tab over comma because paper titles routinely contain
  commas. Configurable via `sep=`.
- **Cell encoding — minimal-quoting variant**. The audience is
  an LLM, not a parser; rules are tuned for token frugality:
  - `None` → empty string.
  - Booleans → `true` / `false` (lowercase).
  - Numbers → `str(int)` / `repr(float)` (`repr` keeps precision
    when displayed). Numbers are emitted **unquoted**.
  - Strings → as-is unless they contain the separator, a newline,
    or a carriage return — those are the only characters that
    would break the columnar shape an LLM reads off the wire. In
    that case the cell is wrapped in `"…"` and any embedded `"`
    inside the wrapper is doubled (RFC 4180) so the closing
    wrapper boundary stays unambiguous.
  - Bare `"` inside a cell that does not otherwise need wrapping
    is **passed through verbatim**. An LLM reads
    `He said "hi"` as one cell more readily than the wrapped
    `"He said ""hi"""`, and a literal `"` does not break the
    column structure (the boundary is the tab).
  - Any other type → `str(value)` then string-encoded as above.
- **Line terminator** is `\n` (Unix). MCP transport is line-based
  but tolerant; we don't emit `\r\n`.
- **No trailing newline** on the last row, so a one-row dump is
  exactly two `\n`-joined lines.
- **All-empty rows** render as the empty string. We deliberately
  do not force-quote a single-column all-empty row to ``""`` to
  preserve a hypothetical `load(dump(rows))` round-trip — the
  LLM consumer doesn't care, and `""` would just spend tokens
  on shape the wire format does not need to carry.

A `dict` argument is treated as a one-row table. A `list[dict]`
where rows have heterogeneous keys uses the union of all keys for
the header; missing values render as empty cells.

### Why minimal quoting

The output's audience is an LLM. We do not parse our own dumps
in production. The earlier draft (commit 2026-05-22 morning) used
strict RFC 4180 quoting on every cell that contained ``"``, plus
a `""` force-quote on all-empty single-column rows so
``load(dump(rows))`` round-tripped on every shape. That cost real
tokens on common payloads — paper titles with bare quotes,
status messages with quoted strings — for a property the
production path does not exercise. The current rule is
**quote only when the wrapper is necessary to preserve the
columnar shape**.

Trade-off accepted: cells that *start* with a literal `"`
(e.g. `'"already-quoted"'`) cannot be reliably parsed back by
``load()`` because the leading quote looks like a wrapper open.
LLMs read them from context with no problem. The roundtrip test
suite explicitly excludes the four shapes that lose under the
new rule (cell starts with `"`; empty single-column row; cell
that is exactly `""`; ambiguous edge cases). All other shapes
(embedded tab/newline/CR, mid-string quotes, multi-column
all-empty) still round-trip cleanly.

`load(text, sep="\t")` reverses `dump`:

- Returns `list[dict[str, str]]`. Cells always come back as
  strings; type recovery is the caller's job (TOON is a transport,
  not a schema language).
- Tolerates a trailing blank line (common when piping).
- Empty input returns `[]`.

The implementation is pure Python — no extra dependency. ADR 0002
mentioned `toons` / `toon-python` as candidates; we deliberately do
not pull in either because our usage is the simple homogeneous-row
shape and a hand-rolled implementation is ~80 LOC, lock-free, and
tested.

## `SERIALIZERS` registry

`precis/format/__init__.py` exposes:

```python
SERIALIZERS: dict[str, Callable[..., str]]  # name -> serializer

def serialize(data, *, format: str = "toon", **kwargs) -> str: ...
def register(name: str, fn: Callable[..., str]) -> None: ...
```

`SERIALIZERS` is keyed by short name (`"toon"`, `"json"`, `"table"`).
Out of the box it carries:

- `"toon"` → `precis.format.toon.dump`.
- `"json"` → a thin wrapper around `json.dumps(default=str,
  ensure_ascii=False, indent=2)`. Always available so callers that
  truly need nested-record output have a single import.
- `"table"` → `precis.format.table.render` (a tiny ASCII box-drawing
  renderer, see below). Marked as TTY-oriented; piping it through
  `awk` is not the goal.

`serialize()` is a thin shim:

```python
def serialize(data, *, format="toon", **kwargs):
    try:
        fn = SERIALIZERS[format]
    except KeyError:
        raise ValueError(f"unknown format {format!r}; "
                         f"known: {sorted(SERIALIZERS)}")
    return fn(data, **kwargs)
```

The registry exists so a future format (e.g. ndjson) lands as one
line in `__init__.py`, not a sweep across every caller.

## `--format` flag — CLI integration contract

`precis.cli._common` grows:

```python
def add_format_argument(parser: argparse.ArgumentParser) -> None: ...

def resolve_format(
    args: argparse.Namespace,
    *,
    default_tty: str = "table",
    default_pipe: str = "toon",
) -> str: ...
```

- `add_format_argument` registers `--format {toon,json,table}` with
  `default=None`. Subcommands that opt in call this and then
  `resolve_format(args)` to pick the effective value.
- `resolve_format`'s precedence is:
  1. The CLI flag value if set.
  2. `default_tty` if `sys.stdout.isatty()`.
  3. `default_pipe` otherwise.

`precis worker --status` opts in. The renderer is one line:

```python
fmt = resolve_format(args)
print(serialize(rows, format=fmt))
```

with `rows` a `list[dict]` built from `handler.status(conn)`. This
replaces the hand-rolled TSV print loop in `_print_status`. The
existing leading `# handler\ttotal\t...` comment line goes away;
agents that need the header read the TOON header row directly.

## `table` renderer

`precis/format/table.py` provides a deliberately tiny ASCII table
renderer for the TTY default. Box-drawing characters, one row per
record, columns sized to the longest cell in each column. No
external dep (we avoid pulling `rich` for one purpose — it's a
~MB-sized transitive that's not justified for status output).

The renderer is roughly:

```
┌──────────────┬─────────┬─────┐
│ handler      │ pending │ ok  │
├──────────────┼─────────┼─────┤
│ embed:bge-m3 │ 10      │ 200 │
└──────────────┴─────────┴─────┘
```

ASCII-only Unicode box-drawing (`U+2500` family) so it renders in
any monospace terminal. No colour. If the registered TTY is dumb
(`TERM=dumb`), we still emit the box characters; making them
optional is more knob than it's worth.

## Skill content — `precis-toon.md`

A short skill explaining the TOON shape to agents:

- "Tabular MCP responses are TOON: first non-blank line is a
  tab-separated header; subsequent lines are rows."
- Snippet showing a Python `csv.DictReader(text, delimiter="\t")`
  fallback that parses TOON without our library.
- Note: cells may be quoted with `"` if they contain a tab,
  newline, or `"`.

Lives at `src/precis/data/skills/precis-toon.md`; gets picked up by
the existing skill index just like every other `precis-*.md`.

## File layout

```
src/precis/format/
  __init__.py            # SERIALIZERS, serialize(), register()
  toon.py                # dump / load
  table.py               # ASCII box-drawing renderer
  _json.py               # thin json.dumps wrapper (for the registry)

src/precis/cli/_common.py
  + add_format_argument(parser)
  + resolve_format(args, *, default_tty='table', default_pipe='toon')

src/precis/cli/worker.py
  - _print_status: replace TSV print loop with serialize(rows, format=fmt)
  + add_format_argument(p) in add_parser

src/precis/data/skills/precis-toon.md   (new)

tests/format/
  test_toon_dump.py
  test_toon_load.py
  test_toon_roundtrip.py
  test_registry.py
  test_table.py
  test_resolve_format.py

tests/test_worker_cli.py
  + test_status_default_pipe_emits_toon
  + test_status_format_table
  + test_status_format_json
```

## Test plan

Tests go first for new pure-library code.

**Unit (no DB)**

- `test_toon_dump.py`: empty list → `""`; one row → header + one
  line; multi-row → header + N lines; `None` → empty cell;
  booleans → `true`/`false`; floats round-trip via `repr`; cells
  with tabs / newlines / quotes are quoted and escaped; explicit
  `schema=` pins column order even when keys are missing in rows;
  heterogeneous rows union the keys in first-seen order;
  non-string scalars stringify; `sep=","` changes the delimiter.
- `test_toon_load.py`: parse a header + 2 rows; quoted cells are
  unquoted; doubled quotes unescape to a single quote; trailing
  blank line is tolerated; empty string → `[]`; CR/LF tolerated;
  cells preserve their string form (no numeric coercion).
- `test_toon_roundtrip.py`: `load(dump(rows)) == rows-as-strings`
  for a curated set of cells covering every escape case. Hypothesis
  strategy for fuzzing the roundtrip on a constrained alphabet of
  printable Unicode.
- `test_registry.py`: `SERIALIZERS` contains at least `toon` /
  `json` / `table`; `serialize(rows, format='toon')` equals
  `toon.dump(rows)`; `serialize(rows, format='nope')` raises
  `ValueError`; `register('ndjson', fn)` adds an entry that
  `serialize` then picks up.
- `test_table.py`: a 0-row table renders only the header row in a
  box; a 1-row table renders header + separator + row; column
  widths follow the longest cell; non-string scalars stringify.
- `test_resolve_format.py`: `--format toon` overrides TTY default;
  no flag + TTY → `table`; no flag + pipe → `toon`; override
  defaults are honoured.

**Integration (with DB)**

- `tests/test_worker_cli.py` gains three tests that monkeypatch
  `sys.stdout` to mark TTY or not and assert the rendered shape:
  `default_pipe_emits_toon`, `format_table`, `format_json`. The
  rows come from a seeded mock-bge-m3 + RAKE handler against the
  shared worker DB fixture.

Existing tests that asserted the leading `# handler\ttotal\t...`
comment line in worker output get updated to the TOON header
form (no `#`).

## Risks / open follow-ups

- **Tab handling in titles** — the escape path is exercised in
  unit tests; in practice paper titles don't contain tabs, but a
  malformed PDF metadata extract could.  The CSV-style quoting is
  the safety net.
- **Handler refactor for MCP tabular surfaces** — once the format
  library is in, a follow-up plan can refactor `_render_list_papers`
  / search hit rendering to go through `serialize()`. Tracked as
  `OPEN-ITEMS.md` `B10-followup`.
- **`rich` vs hand-rolled table** — if the table renderer grows
  past ~80 LOC or needs colour, the right move is to pull `rich`
  as a `[cli]` extra rather than reinventing more. Not today.

## Definition of done

- [ ] `src/precis/format/{__init__,toon,table,_json}.py` exist and
      type-check.
- [ ] `precis worker --status | cat` emits a TOON document with a
      header row.
- [ ] `precis worker --status` on a TTY emits an ASCII table.
- [ ] `precis worker --status --format json` emits JSON.
- [ ] All new tests pass; pre-existing worker tests updated.
- [ ] `precis-toon.md` skill lands and is picked up by the skill
      index (lints clean).
- [ ] `CHANGELOG.md` gets a "B10 — TOON output" entry under
      `Unreleased` (or under v7.0.0 if still open).
- [ ] `docs/design/pip-merge.md` §B10 marked ✅ with a summary.
