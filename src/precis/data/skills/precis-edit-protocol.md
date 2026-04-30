---
id: precis-edit-protocol
title: precis — anchored edits across every file kind
status: shipped (v1) — markdown + python; other file kinds queued
tier: 2
floor: any
applies-to: put with mode='edit' or mode='insert' on R/W file kinds (markdown + python today; plaintext, rmk, docx, tex, book queued)
last-updated: 2026-04-30
---

# precis-edit-protocol — sub-region anchored search/replace

For changes smaller than a whole region, use `mode='edit'` instead
of `mode='replace'`. The grammar is **identical across every file
kind**; only the validation gates differ. Per-kind quirks (cross-
region rules, AST gates, paragraph integrity) live in each kind's
skill — this one covers what's universal.

## When to reach for it

| You want to | Use |
|---|---|
| Rewrite a whole block / function / paragraph | `mode='replace'` |
| Change one token, one cite, one literal | `mode='edit'` |
| Add text adjacent to an existing anchor | `mode='insert'` |
| Bulk rename one identifier | `mode='edit'` with `match='all'` |

The rule of thumb: **content selects, range bounds.** The `id=`
selector narrows where to look; the literal `find=` decides what
to change. This survives drift after prior edits.

## Schema

```python
put(kind='<kind>', id='<path>[~<selector>]',
    mode='edit',          # or 'insert'
    find='<exact text>',  # literal — required
    before='<anchor>',    # optional: text immediately preceding find
    after='<anchor>',     # optional: text immediately following find
    text='<new text>',    # required (use '' on edit to delete the match)
    where='before|after', # required for mode='insert' only
    match='unique',       # unique (default) | first | all | nth
    nth=2)                # 1-indexed when match='nth'
```

The motivating case from the spec:

```python
# 'the fox jumps over the fence.' → 'the fox jumps over a fence.'
put(kind='markdown', id='notes--foo~intro',
    mode='edit',
    find='the', before='over ', after=' fence',
    text='a')
```

## Resolution algorithm

Identical for every kind:

1. **Resolve the region** from `id` (whole file, block, qualname,
   or line range — the standard precis address grammar).
2. **Find candidate matches** of `find` inside the region.
3. **Anchor filter** — drop candidates whose surrounding bytes
   don't equal `before=` / `after=`. Whitespace is **strict**.
4. **Apply `match` policy**:
   - `unique` (default) — exactly 1 match required.
   - `first` — earliest match wins.
   - `all` — every match.
   - `nth` — the Nth match (1-indexed).
5. **Splice → kind-specific validate → format → atomic write → re-ingest.**

If any step fails, disk is untouched.

## Errors — every one is actionable

### `find` not found

The error includes up to 3 fuzzy nearest lines so the agent has
something concrete to fix:

```
find='dpoamine' not found in notes--neuroscience~abstract
Nearest matches in the region:
  L42  dopamine is a neurotransmitter   (88% similar)
```

Next: widen the `id=` to a larger region, or copy the exact text
from `get(... view='raw')`.

### Ambiguous (`match='unique'` had ≥2 hits)

Every candidate is listed with its line number:

```
find='the' has 3 matches in notes--foo~intro (match='unique' requires exactly 1):
  L42  The fox jumps over the fence.
  L43  The morning was clear.
```

Next: narrow with `before='...'` / `after='...'`, or pick a policy
(`match='all'` / `match='nth'` with `nth=N`).

### Post-edit validation

Kind-specific gates run on the spliced buffer before any disk
write. Failure rolls back; disk stays unchanged.

```
ast.parse failed on the post-edit buffer: SyntaxError: ... (line 142)
Next: check the indentation / syntax of the replacement text
```

For python this fires for `ast.parse` failures and for the
qualname-drop check (an edit that renames a `def` is rejected
unless you pass `allow_rename=True`).

## Why anchors instead of regex

Two-anchor + literal `find` covers most cases regex covers, with
three advantages:

- **No escape hazards.** Models do not reliably escape `.`, `*`,
  `(`, `\`. Literal anchors sidestep this.
- **Self-documenting.** `before='over '` reads as "after the word
  over"; `\boverthe\b` does not.
- **Bounded ambiguity.** Regex can match in surprising places;
  literal text + before/after cannot.

Regex / `dry_run` / multi-edit batches are **deferred to v2** —
the v1 surface is intentionally minimal.

## Per-kind quirks

The schema is universal; the validation differs:

| Kind | Status | Validation gate | Format step |
|---|---|---|---|
| `markdown` | **shipped v1** | re-parse blocks on re-ingest | none |
| `python` | **shipped v1** | `ast.parse` + qualname-stable | `ruff check --fix` + `ruff format` |
| `plaintext` | queued | none | none |
| `rmk` | queued | re-parse | trim trailing ws |
| `docx` | queued | re-parse XML; run integrity | none |
| `tex` | queued | brace + env balance | none |
| `book` | queued | delegated per child file | delegated |

For kind-specific examples and recipes, see the kind's own skill
(`precis-markdown-help § Surgical edits`, `precis-python-help §
Anchored edits`).

v1 does **not** include the explicit cross-region rejection
(matches that span markdown blocks or python top-level statements
are allowed; the kind's own validation gate catches the breakage
that would result). A future version may add an opt-out
`allow_cross_region=False` knob if data shows it's needed.

## What this protocol does NOT do (v1)

- **Regex.** Literal `find=` only. Deferred to v2 with `regex=True`.
- **Multi-edit batches.** One edit per call. `edits=[…]` is v2.
- **`dry_run`.** Defer to v2; for now, edits go straight through.
- **vi-style modal commands.** Not even in v2 — kept out of the
  protocol layer. If a client wants `:s/old/new/` sugar, it
  compiles to this schema before reaching the handler.
- **Cross-file edits.** One `id` per `put`. Sequence multiple calls
  (or use `quest` / `sortie`) for multi-file refactors.
- **Fuzzy `find`.** Exact match. Fuzzy lives only in the
  *suggestion* leg of error messages.
- **Cursor / position state across calls.** Stateless.

## See also

- `precis-files-help` — universal address grammar that `id=` uses
- `precis-markdown-help § Surgical edits` — markdown recipes
- `precis-python-help § Anchored edits` — python recipes (AST + ruff gates apply)
- `docs/edit-protocol-spec.md` — full design rationale and v2 roadmap
