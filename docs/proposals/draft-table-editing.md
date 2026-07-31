---
status: draft
title: In-place / field-level table editing for drafts, and a backslash-safe cell channel
model: opus
---

# In-place / field-level table editing for drafts, and a backslash-safe cell channel

## Motivation / why

Two open gripes, filed while fixing a one-character unit-label error
(aJ→zJ) in the `nano-computer` draft, both land on the same surface:
editing a `chunk_kind='table'` chunk is painful.

- **gr178501** — *no lossless / in-place edit.* `text=` and find-replace
  are both rejected on a table chunk; the only edit path is
  `table={header, rows}` (`_edit_table` in `src/precis/handlers/draft.py`,
  guarded by the `is_table` routing). That path forces the caller to
  re-supply the **entire** canonical structure to change one cell, and it
  cannot address a single field.
- **gr178512** — *backslash double-encoding in the `table=` dict.*
  LaTeX cell strings (`$\sim$3~aJ`, `\textsuperscript{a}`) come back with
  **doubled** backslashes and break on export, whereas the sibling
  `caption=` string param round-trips the same single-backslash LaTeX
  correctly in the same call. Passing single-backslash JSON inside the
  dict is invalid JSON and is rejected (`dict_type` error), so there is
  effectively *no input* that stores single-backslash cell content.

**This is not a skill issue.** `precis-draft-help` documents the current
design faithfully and correctly. The design *itself* lacks a field-level
edit path, and one transport path is buggy.

### What the code actually does (diagnosis)

The table pipeline is single-sourced through
`src/precis/utils/table_data.py` (ADR 0035 §1), and — importantly — the
canonical store is **format-neutral structured data**, not any markup:

- **Store.** `normalize_table` keeps cells as JSON scalars verbatim in
  `meta.table = {header, rows}`. There is **no LaTeX stored** — LaTeX is
  emitted only at export.
- **Derive.** `table_to_markdown` → `_cell_md` escapes cells for the GFM
  `text` projection (`\` → `\\`, `|` → `\|`); reversed by `_uncell_md` on
  the markdown-parse fallback.
- **Export — both formats build native output from the same structured
  payload:**
  - LaTeX/PDF: `_render_table` in `src/precis/export/latex.py` reads
    `table_payload` (unescaped `cell_text`) → a `longtable`, each cell
    through `_render_inline` (prose grammar: `$…$` math survives, a bare
    backslash outside math is text-escaped by `_latex_escape`).
  - Word/.docx: `_render_table` in `src/precis/export/docx.py` builds a
    **native** `add_table`, `$…$` → native Word equations via
    `latex_to_omml`.

The decisive fact for gr178512: `caption=` and a table cell both reach
export through `_render_inline`, yet only the cell doubles its
backslashes. The divergence therefore happens **before storage**, at the
dict-param decode boundary — a top-level string arg and a string *inside a
dict arg* are not decoded with the same backslash semantics. That boundary
is largely client-side (how the MCP client serialises nested-object tool
args), which is why the gripe could not pin it from server code alone.

Strategic takeaway: **string-typed params round-trip backslashes
reliably; dict-nested strings do not.** Route cell content through string
channels; harden or avoid the dict path.

### Why we do NOT store LaTeX (answering the tempting fix)

The obvious "lossless" fix — store hand-tuned LaTeX tabular source and
emit it verbatim — is wrong for this system, because precis exports to
**both** PDF and Word from one canonical source. Raw LaTeX serves only
PDF; the docx path (`add_table`) cannot consume it, and reverse-parsing it
back to `{header, rows}` for Word re-flattens exactly the
`\multicolumn`/rules structure that motivated storing it. Format-specific
source breaks the format-neutral contract the whole draft/export system
rests on. The right answer to structural richness is to **extend the
structured schema**, not to store markup (see item 3, deferred).

## In scope

A **layered** fix, cheapest-and-highest-value first:

1. **Field-level, in-place table editing** — the core gap. Two addressing
   modes, both using **string** params so backslashes round-trip
   correctly (sidestepping gr178512 for the common edit):
   - **Find-replace across cells:** `edit(kind='draft', id='dc<id>',
     find='aJ', text='zJ')` (and the regex `sub=` form) locate/replace
     over the canonical cell values in `meta.table`, then re-derive the
     markdown and persist. Direct answer to the aJ→zJ pain: no re-supplied
     structure, zero collateral. A find with no match is refused (chunk
     untouched), mirroring the prose find-replace guard.
   - **Excel-style coordinate set:** `edit(kind='draft', id='dc<id>',
     cell='B2', text='$\sim$3 zJ')` sets one cell without re-sending the
     grid. The **address** is a string (`cell='B2'` A1 notation, or the
     programmatic `cell={row, col}` with 1-based ints — no letter-counting),
     and the **new value always arrives via the top-level `text=` string
     param**, which is what makes the whole path backslash-safe by
     construction (see item 2 — string params round-trip, dict-nested
     strings don't). Row 1 is the header row, so `cell='B1'` renames a
     column. The value is type-inferred back to a JSON scalar (int → float
     → bool/null → str, Excel-style on-entry inference) so a number stays a
     number for the numerics index.
2. **Harden the `table=` dict path against backslash doubling.** ✅ **Shipped
   (2b).** The decode boundary was pinned to the **client** (a regression test
   proves `normalize_table` never doubles a dict cell's backslashes — server
   path is clean), so path (a) — server-side "un-doubling" — was rejected as
   fundamentally lossy: a legitimate `\\` (a LaTeX row-break) is
   indistinguishable from a doubled `\`. Path (b) shipped: `normalize_table`
   now accepts a top-level JSON **string** `table=`, `json.loads`-decoded once
   server-side, so an agent passes it the same reliable way it passes
   `caption=`. Regression: `test_*_string_channel_*` in `tests/test_draft_table.py`.
3. **(Deferred, separate proposal) Structured enrichment for rich
   tables.** Represent column alignment, `\multicolumn`/`\multirow` spans,
   rule placement, and footnote markers as **structured fields on
   `meta.table`**, so both exporters can render them natively. This is the
   format-neutral answer to gr178501's structural-loss complaint. Larger
   and independent of items 1–2; called out here so it is not lost, but it
   should graduate as its own spec.

## Explicitly NOT in scope

- **Storing LaTeX (or any format-specific markup) as the table source.**
  Explicitly rejected — it breaks Word export and the one-canonical-source
  contract (see "Why we do NOT store LaTeX"). Structural richness is
  handled by extending the structured schema (item 3), not by storing
  markup.
- **Wrapping a dataframe / spreadsheet library (pandas, polars, Quadratic)
  for the model or edit engine.** Rejected — see the decision below. The
  canonical model is already dataframe-shaped, the edit ops are ~50 lines,
  and every candidate fights our typed-JSON-scalar contract and/or our two
  native exporters while doing nothing for the backslash bug. A stdlib
  `csv` import/export for interchange is the only "don't reinvent" piece
  worth having, and it is separate from the edit path (not in this slice).
- **Abandoning the canonical `{header, rows}` model.** It stays the source
  of truth and keeps numerics indexable (ADR 0035 §1).
- **Free-form `text=` rewrite of a derived table.** We do not un-reject
  plain `text=` on a table chunk — that reintroduces the derived-vs-canonical
  drift the design prevents. Editing goes through find-replace or
  coordinate addressing on the canonical data.
- **A rich web table editor / spreadsheet grid.** Out of scope; this is
  the MCP/programmatic edit path. A web cell editor could later layer on
  item 1.
- **Fixing MCP client-side JSON encoding.** If the doubling proves purely
  client-side, the server-side mitigations (items 1 and 2) are the
  deliverable; we do not ship a patched client.

## Acceptance criteria

- `edit(kind='draft', id='dc<id>', find='aJ', text='zJ')` on a
  `chunk_kind='table'` chunk changes only the matching cell(s), re-derives
  the markdown, and leaves every other cell and the caption untouched —
  verified on a table with a non-target cell that must not change. A find
  with no match is refused (chunk untouched).
- A single field can be edited by coordinate without re-supplying the
  grid, and a numeric cell edited to a number stays a JSON number in
  `meta.table` (not stringified) — verified against the numerics path.
- A table cell set to single-backslash LaTeX (`$\sim$3 aJ`) via the
  supported path stores single-backslash content in `meta.table` **and**
  round-trips through **both** exporters without corruption — LaTeX emits
  `$\sim$` (not `$\\sim$`, not `\textbackslash{}`), and the docx cell
  carries the intended content (math as a native equation).
- Both gripes (gr178501, gr178512) are closable by pointing at the
  regression test(s) above.

## Target + blast radius

- `src/precis/handlers/draft.py` — the `edit` routing (`is_table`
  branch) and `_edit_table`; item 1 adds find-replace + coordinate paths
  for table chunks instead of the current unconditional reject.
- `src/precis/utils/table_data.py` — `normalize_table` (item 2
  normalisation; item 1 coordinate mutate + re-derive helper),
  `table_to_markdown`/`table_payload`.
- `src/precis/tools/core.py` — `put`/`edit` `table=` (item 2b JSON-string
  acceptance) and any new `cell=`/coordinate param surface.
- `src/precis/export/{latex,docx}.py` — **read-only for items 1–2**
  (both already build from the structured payload); touched only if item 3
  (structured enrichment) graduates.
- `src/precis/data/skills/precis-draft-help*` — document field-level table
  editing + cell content conventions.
- Governed by **ADR 0035 §1**; item 3, if it graduates, amends that ADR
  (richer structured table schema).

## Open questions / decisions log

- **DECIDED — gr178512's decode boundary is client-side.** Pinned by test,
  not a live repro: `test_normalize_table_dict_form_never_doubles_backslash`
  passes a real Python dict with a single-backslash cell straight to
  `normalize_table` and the cell comes back unchanged — the server never
  touches backslashes, so the doubling is purely the MCP client serializing a
  nested-dict arg. That rules out a server-side contributor (so item 2a
  normalisation is both unnecessary and unsafe) and confirms the 2b string
  channel as the fix — a top-level JSON-string `table=` rides the same
  reliable path as `caption=`. Shipped.
- **DECIDED — coordinate addressing surface.** `cell=` string address
  (`'B2'` A1 notation) or `cell={row, col}` (1-based ints), with the new
  value on the top-level `text=` string param (keeps it backslash-safe).
  Row 1 is the header row (so `cell='B1'` renames a column); value
  type-inferred on entry (int → finite float → bool → str; a non-finite
  `NaN`/`inf` stays a string, and an empty string stays `''` — no null
  inference). To pin a numeric-looking value as a string (e.g. `'007'`),
  use the full `table=` payload, which carries explicit JSON scalar types. Chosen over a `#r2c1` id-grammar (overloads the handle) and a
  `column={name, values}` bulk form (deferred — cell + find-replace cover
  the reported pain; a column op can follow).
- **DECIDED — no dataframe/spreadsheet library.** Not pandas, polars, or
  Quadratic. Rationale: (1) our `{header, rows}` canonical model is already
  a dataframe, and the needed ops (set cell, add/drop row/col, rename
  header) are trivial list-of-lists mutations; (2) pandas coerces types
  (int→float on null, mixed→object) and fights the typed-JSON-scalar
  contract the numerics index depends on; polars enforces one dtype/column,
  which our heterogeneous cells violate; (3) neither helps rendering — we
  already emit *native* LaTeX (`longtable`+booktabs) and *native* Word
  (`add_table`+OMML), and `.to_latex()` would regress that with no
  `.to_docx()` at all; (4) a library does nothing for the backslash bug
  (gr178512), which is an MCP arg-encoding issue upstream of any model.
  Quadratic is a browser spreadsheet *app* (its own UI + storage), not a
  wrappable library — a future web grid-editor surface, not this slice. A
  real compute engine is only justified if/when we add *formula cells*, and
  that slots into the existing figure/`calc` compute lane (ADR 0035 §2/§3),
  not the edit path. Interchange (CSV/Excel import) uses stdlib `csv`, kept
  separate.
- **find-replace scope on a table.** Cells only by default (caption has
  its own `caption=` edit); the section-scoped regex `sub=` form may opt
  the caption in. Decide when wiring.
- **Item 3 is a separate deliverable.** Items 1+2 solve the reported pain
  and are tightly coupled; item 3 (structured enrichment) is independent
  and larger. It should be split into its own proposal — the `ready` gate
  should flag this if left combined.
