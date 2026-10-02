---
status: draft
pillar: memory-graph
---

# Draft hygiene lints

Grouped 2026-09-26 from 3 items that are sub-parts of one deliverable (each keeps its own section below; the originals are in the history). Split a section back out only when it becomes independently shippable.

## Notation-drift checker for drafts

_Grouped 2026-09-26; was `draft-notation-consistency`, status draft._

### Motivation / why
On the nanobuds draft the same fullerene appeared three ways across sections —
plain `C60`, Unicode `C₆₀`, and LaTeX `C$_{60}$` — and coined terms drifted in
casing (`NanoBud` vs `nanobud`) and abbreviation (`SWNT` vs `SWCNT`). This is
partly a **rendering correctness** bug, not just cosmetics: plain `C60` exports
to literal "C60" (no subscript) in LaTeX while `C$_{60}$` renders correctly, so
a drifted draft ships visibly inconsistent formulae. Detecting it by hand meant
an agent grepping `\bC\d{2,3}\b`, Unicode subscripts, and casing variants
separately, then reconciling. All of it is decidable by compute.

### In scope
- A whole-draft pass that groups surface forms by normalized identity and flags
  any identity with >1 surface form:
  - **Subscript species**: normalize `C60`, `C₆₀` (Unicode subscripts),
    `C$_{60}$` to one key; same for any `X<digits>` (B12N12, WS2). Report the
    variants and their chunks; recommend the LaTeX form as canonical.
  - **Casing/abbreviation drift** for coined terms: case-insensitive grouping
    flags `NanoBud`/`nanobud` and `SWNT`/`SWCNT` co-occurring. Cross-reference
    the term registry / glossary so a trademarked form (Canatu `NanoBud™`) and
    a defined variant-abbreviation entry are allow-listed, not flagged.
- Surface in the Hygiene footer (a "notation drift: N species / M terms" line),
  same seam as the house-style lint.
- A ready-to-run normalization suggestion (the regex sub that fixes it) — the
  `edit(sub={…})` backref form `\bC(\d{2,3})\b → C$_{\1}$` already does this
  cleanly and could be surfaced as the offered fix.

### Explicitly NOT in scope
- Auto-normalizing. Offer the sub; don't apply it (a "C20" might be a matrix
  label, not a fullerene — human confirms).
- General English spelling consistency — this is about coined/technical tokens
  and subscript species only.

### Acceptance criteria
- A draft mixing `C60`, `C₆₀`, `C$_{60}$` yields one grouped finding naming all
  three forms + their chunks, with `C$_{60}$` recommended.
- `NanoBud™` (trademark, in the glossary) does NOT trip the casing check while a
  generic `NanoBud` in running prose does.
- Clean draft → all-clear line.

### Target + blast radius
`src/precis/handlers/draft.py` (Hygiene footer); term-registry / glossary
lookup for the allow-list; new pure helper. No schema change.

### Open questions / decisions log
- Canonical-form policy per journal (LaTeX vs Unicode subscripts): fixed to
  LaTeX, or a draft-level `meta` toggle? Default LaTeX (renders everywhere).

## Near-duplicate chunk detector for drafts

_Grouped 2026-09-26; was `draft-duplicate-chunk-detector`, status draft._

### Motivation / why
The nanobuds coherence pass found three content duplications a per-section
review structurally cannot catch: a whole Sensing paragraph in Future
Perspectives recapping the Applications section (both citing the same finding),
the Nicholls in-situ result stated in both Synthesis and Future, and a
graphene-electrochemical sentence misplaced+duplicated across two subsections.
Finding these needed a whole-document agent read. But every draft chunk is
**already embedded** (the reactive embed on write) — a cosine pass over the
chunk vectors would surface duplicate/near-duplicate paragraph pairs for near
zero cost, turning an expensive agent read into a cheap deterministic report.

### In scope
- A `view='duplicates'` (or a Hygiene footer line) that computes pairwise
  similarity over the draft's prose-chunk embeddings and lists pairs above a
  threshold, most-similar first, with both `dc<id>` handles and their section
  paths so "same claim in two sections" is obvious.
- Flag intra-section near-dupes (likely redundancy) distinctly from
  cross-section (likely a recap that belongs in one place).
- Cite-overlap boost: two chunks citing the same `[fi…]`/`[pc…]` AND textually
  similar rank higher (the recap signature).

### Explicitly NOT in scope
- Auto-merging or deletion — report only; the author decides which copy stays
  (deleting authored paragraphs is a human call).
- Cross-draft dedup — scope is one draft.
- Table/figure chunks — prose only.

### Acceptance criteria
- On the nanobuds draft (pre-cut), the B9/B12 recap pair and the Nicholls pair
  both appear in the top results; unrelated paragraphs do not.
- Runs from stored embeddings with no re-embed; whole-draft in well under the
  cost of an agent read.

### Target + blast radius
New read-only draft view in `src/precis/handlers/draft.py`; reads existing
chunk-embedding rows. No schema/migration, no write path.

### Open questions / decisions log
- Threshold + max-pairs default (tune against a few real drafts to keep the
  report signal-dense).
