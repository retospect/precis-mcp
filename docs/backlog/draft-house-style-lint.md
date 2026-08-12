---
status: draft
title: Math-aware house-style lint for drafts (em-dash / bold / italic / double-hyphen) as a write-hint + Hygiene footer
---

# Math-aware house-style lint for drafts

## Motivation / why
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

## In scope
- A pure function over one chunk's text: strip `$…$` and `$$…$$` spans, then
  flag, with line/col spans: em-dash `—` (U+2014); `**bold**`; a single
  `*word*` italic; `_italic_`; ` -- ` / `word--word` double-hyphen.
- Wire it into the two existing surfaces in `src/precis/handlers/draft.py`:
  a **write-time hint** (like the temperature hint) on `put`/`edit`, and the
  **Hygiene footer** line (alongside undefined-abbreviation / whole-paper-cite
  counts) so a whole-draft audit is one `get(view='hygiene')`.
- Each flag names the offending span and the fix (em-dash → sentence split /
  colon / comma; `**x**` → `x`; ` -- ` → `,` or `:`).

## Explicitly NOT in scope
- Auto-fixing. This surfaces; the author/agent decides (an em-dash inside a
  reproduced reference title may legitimately become a colon, not a comma).
- Export-gating. Keep it advisory first; a hard export gate can be a later,
  separate toggle once false-positive rate is known.
- Temperature/unit formatting — already shipped; this rides beside it.

## Acceptance criteria
- On a chunk containing `$P_5$` and `$\mu_B$` and `g-C$_3$N$_4$`: zero
  false positives.
- On a chunk containing `tailor -- and improve --` and `**best**` and a real
  em-dash: three distinct flags with correct line/col.
- `get(kind='draft', id=<slug>, view='hygiene')` shows a house-style line with
  per-rule counts; a clean draft shows the all-clear.

## Target + blast radius
`src/precis/handlers/draft.py` (write-hint assembly + Hygiene footer builder);
new pure helper (near `src/precis/utils/abbreviations.py`, the sibling
deterministic prose check). Rules sourced from
`docs/conventions/llm-facing-prose.md`. No schema/migration.

## Open questions / decisions log
- Inline-code / verbatim spans: does the draft prose model have any `` `…` ``
  convention that should also be exempted like math? Confirm before shipping
  the tokenizer.
