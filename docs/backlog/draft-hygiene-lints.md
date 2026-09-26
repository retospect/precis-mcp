---
status: draft
---

# Draft hygiene lints

Grouped 2026-09-26 from 3 items that are sub-parts of one deliverable (each keeps its own section below; the originals are in the history). Split a section back out only when it becomes independently shippable.

## Math-aware house-style lint for drafts

_Grouped 2026-09-26; was `draft-house-style-lint`, status draft._

### Motivation / why
The prose house-style rules in `docs/conventions/llm-facing-prose.md` (no
em-dash `—`, no `**bold**` / `*italic*` / `_italic_`, no `--` double-hyphen)
are today enforced only by (a) agents remembering to run
`search(kind='draft', mode='regex', …)` by hand and (b) reviewers eyeballing.
A pre-submission pass on `dr173020` (nanobuds) found stray markup this way,
one regex at a time — pure deterministic work an agent should never spend
tokens on. The `temperature/unit` write-hint already proves the pattern:
a cheap, deterministic check that fires on write and in the Hygiene footer.
These three rules are equally decidable and belong in the same seam.

The one non-trivial part — and why this is a real item, not a one-liner — is
that the check must be **math-aware**: a bare `_` inside `$…$` / `$$…$$`
(subscripts like `$P_5$`, `$E_F$`, `$\mu_B$`, `g-C$_3$N$_4$`) is legitimate
LaTeX, not italic markup. A naive `_\w` grep flags ~44 false positives on the
nanobuds draft (all math). The linter must tokenize out math spans before
applying the prose rules.

### In scope
- A pure function over one chunk's text: strip `$…$` and `$$…$$` spans, then
  flag, with line/col spans: em-dash `—` (U+2014); `**bold**`; a single
  `*word*` italic; `_italic_`; ` -- ` / `word--word` double-hyphen.
- Wire it into the two existing surfaces in `src/precis/handlers/draft.py`:
  a **write-time hint** (like the temperature hint) on `put`/`edit`, and the
  **Hygiene footer** line (alongside undefined-abbreviation / whole-paper-cite
  counts) so a whole-draft audit is one `get(view='hygiene')`.
- Each flag names the offending span and the fix (em-dash → sentence split /
  colon / comma; `**x**` → `x`; ` -- ` → `,` or `:`).

### Explicitly NOT in scope
- Auto-fixing. This surfaces; the author/agent decides (an em-dash inside a
  reproduced reference title may legitimately become a colon, not a comma).
- Export-gating. Keep it advisory first; a hard export gate can be a later,
  separate toggle once false-positive rate is known.
- Temperature/unit formatting — already shipped; this rides beside it.

### Acceptance criteria
- On a chunk containing `$P_5$` and `$\mu_B$` and `g-C$_3$N$_4$`: zero
  false positives.
- On a chunk containing `tailor -- and improve --` and `**best**` and a real
  em-dash: three distinct flags with correct line/col.
- `get(kind='draft', id=<slug>, view='hygiene')` shows a house-style line with
  per-rule counts; a clean draft shows the all-clear.

### Target + blast radius
`src/precis/handlers/draft.py` (write-hint assembly + Hygiene footer builder);
new pure helper (near `src/precis/utils/abbreviations.py`, the sibling
deterministic prose check). Rules sourced from
`docs/conventions/llm-facing-prose.md`. No schema/migration.

### Open questions / decisions log
- Inline-code / verbatim spans: does the draft prose model have any `` `…` ``
  convention that should also be exempted like math? Confirm before shipping
  the tokenizer.

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
