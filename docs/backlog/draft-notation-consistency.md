---
status: draft
title: Notation-drift checker for drafts — same species/term written multiple ways (C60 / C₆₀ / C$_{60}$; NanoBud / nanobud; SWNT / SWCNT)
---

# Notation-drift checker for drafts

## Motivation / why
On the nanobuds draft the same fullerene appeared three ways across sections —
plain `C60`, Unicode `C₆₀`, and LaTeX `C$_{60}$` — and coined terms drifted in
casing (`NanoBud` vs `nanobud`) and abbreviation (`SWNT` vs `SWCNT`). This is
partly a **rendering correctness** bug, not just cosmetics: plain `C60` exports
to literal "C60" (no subscript) in LaTeX while `C$_{60}$` renders correctly, so
a drifted draft ships visibly inconsistent formulae. Detecting it by hand meant
an agent grepping `\bC\d{2,3}\b`, Unicode subscripts, and casing variants
separately, then reconciling. All of it is decidable by compute.

## In scope
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

## Explicitly NOT in scope
- Auto-normalizing. Offer the sub; don't apply it (a "C20" might be a matrix
  label, not a fullerene — human confirms).
- General English spelling consistency — this is about coined/technical tokens
  and subscript species only.

## Acceptance criteria
- A draft mixing `C60`, `C₆₀`, `C$_{60}$` yields one grouped finding naming all
  three forms + their chunks, with `C$_{60}$` recommended.
- `NanoBud™` (trademark, in the glossary) does NOT trip the casing check while a
  generic `NanoBud` in running prose does.
- Clean draft → all-clear line.

## Target + blast radius
`src/precis/handlers/draft.py` (Hygiene footer); term-registry / glossary
lookup for the allow-list; new pure helper. No schema change.

## Open questions / decisions log
- Canonical-form policy per journal (LaTeX vs Unicode subscripts): fixed to
  LaTeX, or a draft-level `meta` toggle? Default LaTeX (renders everywhere).
