---
status: v1 shipped — markdown + python (mode='edit' / mode='insert'); regex/batch/dry_run deferred to v2
applies-to: every R/W file-rooted kind (markdown, plaintext, rmk, docx, tex, book, python)
supersedes: nothing — extends the four-mode surface in `file-kinds-unified-addressing.md`
last-updated: 2026-04-30
---

# Universal anchored-edit protocol for file-rooted kinds

> Add a sub-region anchored search/replace primitive (`edit`) on top
> of the existing four-mode put surface
> (`create` / `append` / `replace` / `delete`), shared across every
> R/W file-rooted kind. The new primitive is content-resolved and
> range-bounded, with one-line error messages that name every
> candidate location. No regex by default. No vi-style modal
> shorthand at the protocol layer.

## Why we need this

Today every R/W kind (markdown, plaintext, rmk, docx, tex, book,
python) accepts four put modes: `create`, `append`, `replace`,
`delete`. `replace` swaps a *whole resolved region* — a markdown
block, a python qualname, a line range. That's the right granularity
for structural edits ("rewrite this paragraph", "replace this
function") but it's far too coarse for surgical fixes:

> The fox jumps over the fence. → The fox jumps over a fence.

Today the agent has to round-trip the whole paragraph text and
rewrite it. That:

- Wastes tokens (the full block ships in *and* out).
- Is risky — the model has to reproduce the unchanged tail
  verbatim, which is exactly the operation models are bad at.
- Loses local provenance — "what changed" is buried in a diff of
  two large strings.

What we want is a primitive that says: *find this exact text in
that region; replace it with that.* Modeled after the
benchmark-winning shape (Aider's `SEARCH/REPLACE` blocks, Diff-XYZ's
anchored ops): **content selects, range bounds, validation
guarantees uniqueness.**

## Design principles

1. **Content resolves the edit. Coordinates bound the search.**
   Line ranges are validation guardrails, not the primary selector.
   This survives drift across formatting changes, prior edits, and
   tokenizer-induced whitespace noise.

2. **Exact match by default. Regex is opt-in.** Models generate
   over-broad regex routinely. Literal `find=` is the safe default;
   `regex=True` is power-user mode.

3. **Anchors disambiguate, they don't navigate.** `before=` /
   `after=` are filters on candidate matches, not a way to address
   regions. Use the existing selector grammar for that.

4. **One missing match → one actionable error.** When the find
   text doesn't appear, return up to five fuzzy candidates with
   their line numbers. When it appears multiple times, return every
   location and ask for an anchor or a `match=` policy.

5. **Atomic and immediate.** Same write pipeline as the existing
   modes — splice in memory → kind-specific gates → tmpfile +
   fsync + os.replace + dir-fsync → re-ingest under write lock.

6. **Per-kind hooks, shared spine.** The anchored search/replace
   logic lives in `_FileHandlerBase`. Each kind plugs in:
   - `validate(buffer)` — AST for python, paragraph integrity for
     docx, etc.
   - `forbid_cross_region(span, buffer)` — markdown blocks and
     docx paragraphs reject matches that cross structural
     boundaries by default.
   - `format(buffer)` — ruff for python, none for plaintext.

7. **No vi shorthand at the wire layer.** Modal commands carry
   hidden state (cursor position, mode, counts) that the model has
   to maintain across calls. Worse for benchmarks, worse for error
   messages. If we ever ship vi-like sugar, it compiles to this
   schema before reaching the handler.

## The new modes

The existing four modes stay. Two new modes join them:

| Mode | Region selector (`id~…`) | `find=` | `text=` | Effect |
|---|---|---|---|---|
| `create` | path only | — | required | new file |
| `append` | path only | — | required | append paragraph at end |
| `replace` | required | — | required | swap whole resolved region |
| `delete` | required | — | — | drop whole resolved region |
| **`edit`** | optional (defaults to whole file) | required | required | anchored sub-region replace |
| **`insert`** | optional | required (anchor) | required | insert before/after a found anchor |

`edit` is the workhorse. `insert` is sugar for "I have an anchor
and want to add adjacent text without rewriting the anchor."

## Schema

```python
EditOp = TypedDict("EditOp", {
    # required
    "op": Literal["create", "append", "replace", "delete", "edit", "insert"],
    "id": str,                      # standard precis id grammar

    # for `edit` and `insert`
    "find": NotRequired[str],       # exact text to locate (or regex when regex=True)
    "before": NotRequired[str],     # anchor immediately preceding `find`
    "after": NotRequired[str],      # anchor immediately following `find`

    # for replacement-like ops
    "text": NotRequired[str],       # new text to write

    # `insert` only
    "where": NotRequired[Literal["before", "after"]],

    # match policy (default: "unique")
    "match": NotRequired[Literal["unique", "first", "all", "nth"]],
    "nth": NotRequired[int],        # 1-indexed when match="nth"

    # advanced
    "regex": NotRequired[bool],     # default False
    "flags": NotRequired[list[Literal["i", "m", "s", "x"]]],

    # safety / preview
    "dry_run": NotRequired[bool],   # show diff, don't write
    "expect_lines": NotRequired[int],  # assert resolved span line count
})
```

The handler accepts a single op or a list:

```python
put(kind='markdown', id='notes/foo.md', edits=[
    {"op": "edit", "find": "the", "before": "over ", "after": " fence",
     "text": "a"},
    {"op": "insert", "find": "## Conclusion", "where": "after",
     "text": "\n\nThis was edited atomically."},
])
```

A list is applied **in order, atomically**: all edits succeed and
the file is rewritten once, or none apply and the file is untouched.
Each subsequent edit sees the buffer as updated by the prior ones.

## Resolution algorithm (`edit` and `insert`)

Given `id`, `find`, optional `before`/`after`, and a match policy:

1. **Resolve the region.** Parse `id` per the existing grammar:
   - bare path → whole file
   - `path~slug` → that block's text only (markdown, rmk)
   - `path~Symbol.name` → that symbol's source range (python)
   - `path~L42-58` → those lines

2. **Find candidate matches.** Search `find` (literal or regex)
   against the region buffer. Record `(start_byte, end_byte,
   line_no)` for each hit.

3. **Anchor filter.** Drop candidates whose immediately-preceding
   bytes don't equal `before=` or whose immediately-following bytes
   don't equal `after=`. Whitespace is **strict by default** — no
   normalization. Add a `whitespace="relaxed"` knob in v2 if needed.

4. **Cross-region check.** Reject matches that cross a kind-specific
   structural boundary (markdown block, docx paragraph, python
   class) unless the caller passed `allow_cross_region=True`.
   Default refusal protects formatting and AST integrity.

5. **Apply match policy.**
   - `unique` (default): require exactly 1 candidate. Else
     `BadInput` listing every candidate's line number.
   - `first`: take the earliest. Warn (not error) when ≥2.
   - `all`: replace every candidate.
   - `nth`: 1-indexed pick. `BadInput` when out of range.

6. **Splice.** Compute the new buffer.

7. **Validate.** Run kind-specific gates on the new buffer:
   - python: `ast.parse(buf)` must succeed; no qualname dropped
     unless `allow_rename=True`.
   - docx: every paragraph still parses; runs intact.
   - tex: balanced braces / `\begin{}\end{}`.
   - markdown / plaintext / rmk: parser rebuilds without raising.
   Failure → revert, return `BadInput` with the gate's message.

8. **Format.** Kind-specific. Python runs ruff. Markdown trims
   trailing whitespace on edited lines only. Plaintext: none.

9. **Write.** Atomic tmpfile + fsync + os.replace + dir-fsync.

10. **Re-ingest.** Force the file's blocks to be re-parsed under
    write lock so the next `get` sees the new state.

11. **Respond.** Include the resolved Track-A coordinates of every
    edit (lines that changed, line delta), the resolved Track-B
    name(s), and a one-line summary per edit.

## Errors — actionable, every time

The point of this protocol is that errors **tell you exactly what
to do next**. Three error shapes cover almost everything:

### `BadInput("text not found")`

```
edit failed: find='\\cite{foo2020}' not found in paper.tex~L120-180

Nearest matches in the region:
  L137  \\cite{foo2021}   (1-char diff)
  L152  \\cite{bar2020}   (3-char diff)

Try one of:
  - widen the range:   id='paper.tex'
  - update find=:      find='\\cite{foo2021}'
  - use search first:  search(kind='tex', q='foo2020', scope='paper.tex')
```

### `BadInput("ambiguous: N matches")`

```
edit failed: find='the' has 3 matches in notes/foo.md~intro
  L42  "The fox jumps over the fence."
  L42  "...over [the] fence." ← match #2
  L43  "[The] morning was clear."

Disambiguate with:
  - anchor:   before='over ', after=' fence'   → unique
  - policy:   match='all'    → replace every occurrence
  - policy:   match='nth', nth=2
```

### `BadInput("post-edit validation failed")`

```
edit failed: python AST parse error after edit

  in: precis/src/precis/cli.py
  at: line 142  (within the edited region)
  msg: SyntaxError: unexpected indent

The file on disk is unchanged. The proposed buffer was rejected
before write. Inspect with:
  put(... dry_run=True)
```

Each error carries a `next` field in its structured payload so
agents can act without parsing the prose.

## Why anchors instead of regex (by default)

Two-anchor + literal `find` covers ~95% of the cases regex covers
in this domain, with three big advantages:

- **No escape hazards.** Models do not reliably escape `.`, `*`,
  `(`, `\` in dynamically-constructed regex. Literal anchors
  sidestep this entirely.
- **Self-documenting.** `before='over '` reads as "after the word
  over"; `\boverthe\b` does not.
- **Bounded ambiguity surface.** A regex can match in surprising
  places; literal text + before/after cannot.

Regex stays available behind `regex=True` for the genuine cases
(version bumps, structured renames, rg-style scrubs):

```python
put(kind='python', id='precis/src/precis/__init__.py',
    op='edit',
    regex=True,
    find=r'^__version__ = "[^"]+"',
    flags=['m'],
    text='__version__ = "0.3.0"',
    match='unique')
```

The handler validates the regex compiles before searching and
refuses unbounded patterns (`.*` with no anchor) with a hint.

## Why line ranges are bounds, not selectors

The selector layer (`id~L42-58`) restricts where the search runs;
the **content** decides what gets edited. This is the rule that
benchmarks reward and that aligns with the unified addressing spec
(`file-kinds-unified-addressing.md`):

- Coordinates drift under prior edits. Track A is for inputs from
  external tools (grep, stack traces, IDEs). Track B (block slugs,
  qualnames) is for storage.
- Resolving by content makes the edit *idempotent within a
  matching window* — replaying the same call after the file moves
  10 lines down still finds the right place.
- Line ranges as bounds catch one thing: a content match in the
  wrong location. Everyone wins.

## Examples by kind

### markdown

```python
# Single-token swap inside one block, with anchors. Track-B addressing.
put(kind='markdown', id='notes/foo.md~intro',
    op='edit',
    find='the', before='over ', after=' fence',
    text='a')

# Bounded by line range. The lines are guardrails; content selects.
put(kind='markdown', id='notes/foo.md~L40-80',
    op='edit',
    find='draft', text='final',
    match='unique')

# Insert a new paragraph before a heading.
put(kind='markdown', id='notes/foo.md',
    op='insert',
    find='## Conclusion', where='before',
    text='\n## TL;DR\n\nQuick summary here.\n\n')
```

### python

```python
# Rename one call site. AST + ruff gates run automatically.
put(kind='python', id='precis/src/precis/cli.py~_cmd_serve',
    op='edit',
    find='deprecated_call(', text='new_call(',
    match='all')

# Bump a version string with regex.
put(kind='python', id='precis/src/precis/__init__.py',
    op='edit',
    regex=True,
    find=r'^__version__ = "[\d.]+"',
    flags=['m'],
    text='__version__ = "0.3.0"',
    match='unique')

# Multi-edit transaction — both succeed or neither.
put(kind='python', id='precis/src/precis/cli.py', edits=[
    {"op": "edit", "find": "from .old import X", "text": "from .new import X"},
    {"op": "edit", "find": "X.legacy_method(", "text": "X.method(", "match": "all"},
])
```

### tex

```python
# Citation key swap, range-bounded.
put(kind='tex', id='paper.tex~L120-200',
    op='edit',
    find='\\cite{foo2020}', text='\\cite{foo2024}',
    match='all')

# Insert a new \section after an existing one.
put(kind='tex', id='paper.tex',
    op='insert',
    find='\\section{Background}', where='after',
    text='\n\n\\section{Related Work}\n\nText here.\n')
```

### docx

```python
# Word: anchor + replacement, MUST stay within one paragraph.
# Cross-paragraph edits are rejected to preserve run formatting.
put(kind='docx', id='draft.docx~p42',
    op='edit',
    find='2020', text='2024',
    match='unique')
```

### plaintext

```python
# Whole-file edit, content-resolved.
put(kind='plaintext', id='scratch/notes.txt',
    op='edit',
    find='Reto Stam', text='Reto Stamm',
    match='all')
```

## Per-kind validation matrix

| Kind | Cross-region default | Validation gate | Format step |
|---|---|---|---|
| `markdown` | reject across blocks | re-parse blocks | trim trailing ws |
| `plaintext` | reject across blank-line paragraphs | none | none |
| `rmk` | reject across blocks; never edit front-matter via `edit` | re-parse | trim trailing ws |
| `docx` | reject across paragraphs (run integrity) | re-parse | none |
| `tex` | reject across `\begin{}\end{}` envs | brace + env balance | none |
| `book` | delegated per child file | delegated | delegated |
| `python` | reject across top-level statements | `ast.parse` + qualname-stable | `ruff check --fix` + `ruff format` |

For each kind, `allow_cross_region=True` overrides the rejection.
Use sparingly; it's there for legitimate refactors.

## Multi-edit transactions

A single `put()` may carry an `edits=[…]` list. Semantics:

- Edits apply **in list order** to the same in-memory buffer.
- Each edit's resolution sees the cumulative state from prior
  edits in the list.
- Validation (AST/parse) runs once at the end, on the final buffer.
- Either every edit succeeds and the file rewrites once, or the
  list is rejected and disk is untouched.
- The response carries one summary line per edit plus the final
  Track-A/Track-B coordinates of each touched region.

This is the agent-friendly batch form: write three related edits
at once without juggling intermediate file states.

## `dry_run=True`

Returns the diff that *would* be written, plus all resolved
coordinates and validation results, **without touching disk**. The
response shape matches a successful write but the body is a unified
diff of the proposed change. No re-ingest. No fsync.

```python
put(kind='python', id='cli.py', op='edit',
    find='old', text='new', match='all', dry_run=True)
```

Use it before any large multi-edit batch.

## `expect_lines=N`

Optional safety knob. The caller asserts how many lines the
resolved span (Track A line range) covers. Mismatch → `BadInput`.
Catches "I thought I was editing one line but my id covered ten."

```python
put(kind='markdown', id='foo.md~L42-58',
    op='edit', find='draft', text='final',
    expect_lines=17)   # 58 - 42 + 1
```

Nice-to-have. Skip on a first cut if too costly.

## What we are NOT shipping

These were considered and rejected:

- **vi-style modal shorthand** at the protocol layer. Not even
  optionally — keep it out until there's a measured benchmark win.
  If a future client wants a `:s/old/new/` sugar layer, it
  compiles to this schema before reaching the handler.
- **Whitespace-tolerant anchors by default.** Strict equality is
  predictable; relaxed anchors mask off-by-one bugs. We can add
  `whitespace="relaxed"` later if data shows we need it.
- **Fuzzy `find` matching.** `find` is exact (or regex). Fuzzy
  matching belongs in the *suggestion* leg of the error message,
  not in the executed match.
- **Cursor / position state across calls.** Stateless puts only.
- **Cross-file edits.** One `id` per `put`. Use multiple calls for
  multi-file refactors; sequence them with the existing `quest` or
  `sortie` machinery.

## What benchmarks suggest about model size

The protocol's primary surface (`edit` with literal `find` and
optional `before`/`after`) is sized for the median open-weight
model around 8–35B. Empirical pattern from Aider / Diff-XYZ:

| Model regime | Best primitive | Reason |
|---|---|---|
| Small (≤7B) | `replace` whole region | format burden lowest; failure mode is "doesn't try `edit`"; survives. |
| Mid (8–35B) | `edit` with literal find + 1 anchor | the sweet spot we're optimizing for. |
| Large (frontier) | `edit` with `match=unique`, multi-edit batches, occasional `regex=True` | benchmarks show search/replace beats diff and beats whole-file rewrite for them. |

The same protocol covers all three; small models simply use a
narrower subset of it. We do not ship a separate "small-model
mode" — that's complexity for no win.

## Skill update

A new skill `precis-edit-protocol` documents the universal grammar
in agent-facing form. It cross-references the per-kind skills,
which gain a new "Editing surgically" section pointing at `edit`
and `insert`.

`precis-files-help` adds an "Anchored edits" section after the
existing "Write" section. The four-mode list grows to six.

## Implementation status

### v1 (shipped)

- **`precis.utils.edit_resolve`** — pure resolver. ~440 LOC; 40
  unit tests. `EditOp` dataclass with construction validation,
  `find_candidates` (literal + anchor filter), `select_candidates`
  (match policy with sharp errors), `apply_edit` (splice end-to-
  start). Fuzzy nearest-line suggestions on "find not found"
  errors via sliding-window `difflib.SequenceMatcher`.
- **`MarkdownHandler`** — `mode='edit'` / `mode='insert'` route to
  a new `_put_anchored` helper that resolves the search region
  (whole file or one block), runs `apply_edit`, splices back, and
  re-ingests. 13 integration tests.
- **`PythonHandler`** — same shape; the new helper feeds into the
  existing `_finalize_write()` so the `ast.parse` + qualname-stable
  + ruff gates apply automatically. 12 integration tests.

### Deferred to v2

- `regex=True` + `flags=` — opt-in regex with unbounded-pattern
  guard.
- `edits=[…]` atomic batch transactions.
- `dry_run=True` returning the unified diff without writing.
- `expect_lines=N` safety assertion.
- Explicit cross-region rejection (`allow_cross_region=False`).
  Today markdown allows cross-block matches; the parser re-tokens
  on re-ingest. Python's AST gate already catches the breakage
  cross-statement matches would cause, so the explicit knob isn't
  strictly necessary on the python side either.

### Queued kinds

- `plaintext`, `rmk` — tiny additions once their base R/W lands.
- `docx`, `tex`, `book` — when their R/W shipping. The protocol
  surface and resolver will not change; only the `Region` /
  validation gate per kind.

## Open questions

1. **Whitespace handling for `find=`** — strict (current proposal)
   or `whitespace="strict|relaxed"` knob from day one? Lean strict
   until we see model failures from it.

2. **Anchor length cap** — should we limit `before`/`after` to e.g.
   200 chars to prevent agents from including paragraph-sized
   anchors that defeat the purpose? Lean yes, with an override
   flag.

3. **`edit` on docx runs** — Word paragraphs split text into runs
   when formatting changes mid-paragraph. An `edit` that crosses
   a run boundary has to either (a) re-fuse runs or (b) reject.
   Probably (b) for v1, with a `precis-docx-help` recipe for the
   workaround. Keep this as a docx-specific ticket, not a protocol
   issue.

4. **`replace` on existing kinds** — keep it, even though `edit
   find=<entire region body>` is technically a superset. Reasons:
   (a) `replace` is one operation in agent prose; (b) when the
   region is named (slug or qualname), `replace` doesn't need
   `find=` at all. Keep both.

5. **Conflict-with-other-edits** — should the response include the
   resolved Track-A line ranges *as they are after the write*, or
   as they were resolved? Lean "after". The agent's next call
   should be able to use the returned coordinates verbatim.

## See also

- `file-kinds-unified-addressing.md` — addressing grammar that the
  outer `id` follows.
- `python-kind-spec.md § Write surface` — AST + ruff gates that the
  python `edit` mode inherits.
- `precis-files-help` — current four-mode put surface.
