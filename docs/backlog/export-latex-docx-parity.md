---
status: in-progress
title: LaTeX and DOCX export parity — one shared walker, two renderers
pillar: memory-graph
prio: normal
---

APPROVED by Reto 2026-10-17 (spec stands as written, Stage B included).
Building in `g-export-parity`, one landing per slice. Slices landed: none.

# LaTeX and DOCX export parity

Source: repo-churn report 2026-10-17, item 8 (`.claude/git-churn-2026-10-17.md`
in the `repo-churn` tree).

## Motivation / why

`src/precis/export/latex.py` (3.5k lines) and `src/precis/export/docx.py`
each walk the draft's chunks independently and each re-implement the same
decisions: citation numbering and merging, `[§slug]` / `\cref` cross-reference
resolution, abbreviation first-use expansion, and back-matter ordering. A fix
in one backend leaves the other wrong until someone notices in a real export.

Evidence (git, 2026-10-17 churn window):

- `export/latex.py`: 20 commits, 12 fixes. `export/docx.py`: 16 commits, 10
  fixes. `precis_web/routes/drafts.py`: 17 commits, 11 fixes.
- Eight parity fixes where one backend was brought up to the other, or both
  were changed for the same defect:
  - `588146e28` cite merge, upright formulas, gls collapse (latex + omml)
  - `a5920f7d6` cref polish, back matter, title skip, claims flag
  - `57c5dea70` docx dropped cites, xrefs, equations, bib fields, acronyms
    (+615 lines in docx.py, a catch-up commit)
  - `6aed845fa` docx title block, section depths, paragraph xrefs, back matter
  - `575537a2b` "abbrev parity": nested long forms, definitions in bold
  - `a52d0b29d` docx merges adjacent citations into one mark / EN.CITE field
  - `879bc0406` SI suffix, handle leaks, back-matter order, claims rows
  - `600495757` drop empty groups left by provenance handles (docx + latex)
- Parity already enforced where it was made structural: back-matter order is
  one constant (`export/_back_matter.py` `BACK_MATTER_ORDER`, gr493199) with
  a test per backend (`tests/test_export_back_matter.py`); cite merging has a
  shared helper (`export/_cite_ids.py` `merged_cite_numbers`). The rest is
  per-backend, and no test compares the two.

## In scope

**A shared walker over a renderer protocol (Reto's ruling 2026-10-17: a
pluggable generator, not a materialized IR tree).**

- `export/walker.py`: one walker that visits the draft once. A **numbering
  pre-pass** runs first so forward references resolve (cite numbers, section /
  figure / table / equation numbers, first use of each abbreviation). The
  walker owns every decision: cite numbering and merging, xref numbers,
  abbreviation first use (including nested long forms), back-matter order
  (`BACK_MATTER_ORDER`). It does not build a tree; it calls the renderer as it
  goes.
- A `Renderer` protocol with the methods `cite_group`, `xref`, `term`,
  `heading`, `equation`, `figure`, `table`, `back_matter`, `paragraph`. The
  walker passes resolved values (numbers, labels, definition-or-short flag);
  a renderer only formats them.
- `latex.py` and `docx.py` become renderers with no decision logic. Format
  mechanics stay in them (LaTeX escaping, `\gls`, OMML, EN.CITE fields,
  python-docx runs and bookmarks).
- `g-abbrev-parity` (gr491599) lands first; the walker absorbs its shared
  abbreviation expander.

## Explicitly NOT in scope

- Changing what either backend outputs. The migration is output-preserving,
  proven byte-for-byte (below).
- Other formats (reMarkable, reading edition, audio, EndNote record shapes).
  They can move onto the walker later; no slice here touches them.
- `precis_web/routes/drafts.py` (its fixes are UI/route defects).
- A separate golden-draft parity-summary test (the earlier stage A). Dropped by
  the ruling; the byte-identical safety net and the protocol test below cover
  it.
- Layout equality. Page breaks, fonts and float placement differ by design.

## Size

Medium: roughly 5 builds, one per family below, plus the walker skeleton and
protocol test in the first. The cite family is the largest (merging, EN.CITE,
patent mode, footnote mode); expect the abbreviation family to shrink after
`g-abbrev-parity`.

## Slices

One family per commit, in this order. After each commit, the backend code for
that family contains no decision logic (grep-checkable).

1. **Cites** (plus walker skeleton, numbering pre-pass, `Renderer` protocol
   and the protocol test).
2. **Abbreviations** (after `g-abbrev-parity` merges; absorbs its expander).
3. **Xrefs.**
4. **Back matter** (consumes `BACK_MATTER_ORDER`).
5. **Headings, figures, equations, tables.**

## Acceptance criteria

- **Byte-identical safety net, before and after every commit:** the dr173020
  export and the golden fixture draft give a byte-identical `.tex` and an
  XML-identical docx (compare `word/document.xml` and the other parts after
  canonicalization). The baseline is captured once, before slice 1, and kept
  under `tests/fixtures/export/` for the golden fixture; dr173020 is
  compared by running the export on the pre-commit and post-commit trees
  (it is not checked in).
- All existing per-backend tests (`tests/test_export_docx.py`,
  `tests/test_export_latex.py`, `tests/test_export_back_matter.py`,
  `tests/test_export_provenance_strip.py`) pass **unmodified**.
- **Protocol test:** every `Renderer` method is implemented by both backends,
  and each emits non-empty output for the golden fixture (so a method added
  to the protocol without a renderer fails).
- After slice 5, `latex.py` and `docx.py` contain no citation numbering,
  merge, xref-number or first-use abbreviation logic.
- Outcome, 60 days after slice 5: parity-class fix commits (touching both
  backends, or naming the other backend in the body) fall from 8 in the churn
  window to at most 2.

## Target + blast radius

`src/precis/export/latex.py`, `docx.py`, `omml.py`, `endnote.py`,
`_cite_ids.py`, `_back_matter.py`; new `export/walker.py`; new fixture and
protocol test under `tests/`. Draft export jobs
(`workers/job_types/draft_export.py`), the CLI and the web export panel call
`export_draft` / `export_docx`, whose signatures stay stable. Every submission
export passes through the walker, hence the byte-identical check on each
commit. Docs: `docs/codebase.md` export entry and the `precis.export` package
docstring update when slice 5 ships; delete this file in the same commit.

## Open questions / decisions log

Decisions:

1. **Shape (Reto 2026-10-17):** go ahead with the shared layer now, as a
   pluggable generator (walker + renderer protocol), not a materialized IR
   tree. The earlier "measure first, then decide on the IR" question is closed.
2. **Stage A dropped (same ruling):** no separate parity-summary test; only the
   protocol test plus the byte-identical safety net.
3. **Slice order (same ruling):** cites, abbreviations, xrefs, back matter,
   headings/figures/equations/tables; abbreviations after `g-abbrev-parity`,
   absorbing its expander.

Still open (none blocks starting slice 1):

- The golden fixture draft: built through `DraftHandler` (needs the DB fixture,
  as `tests/test_export_docx.py` does) or from a fake store (as
  `tests/test_export_back_matter.py` does)? Proposal: whichever the shared
  render entry points accept; the baseline files are captured from it.
- How the docx comparison canonicalizes XML (python-docx part blobs through
  `lxml` c14n is the proposal; relationship ids and timestamps in
  `docProps/core.xml` are excluded).
- Modes the walker must carry: patent (`doc_type='patent'`, in-text prior art),
  `footnote_refs` (reMarkable) and `citations='endnote'`. Proposal: all three
  are walker flags the renderer receives, with the fixture covering plain and
  patent, and the endnote and footnote modes covered by the existing tests.
