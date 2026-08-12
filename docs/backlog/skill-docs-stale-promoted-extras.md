---
status: ready
title: Sweep stale [extra] install hints from skill docs after deterministic-tool consolidation
model: sonnet
---

# Sweep stale [extra] install hints from skill docs after deterministic-tool consolidation

<!-- Bounded doc-sync follow-up to the dep consolidation that promoted 7
deterministic no-API extras (calc/tex/docx/mermaid/plot/cad-export/dft) into
core `[project.dependencies]`. No code changed; the extra KEYS are gone. -->

## Motivation / why
The ship that made calc/tex/docx/mermaid/plot/cad-export/dft **core** deps
deleted those extra keys from `pyproject.toml`. The agent-facing skill docs
(served live via `get(kind='skill', …)`) still tell users to
`pip install precis-mcp[dft]` / `[cad-export]` / `[mermaid]` etc. — install
specs that **no longer resolve**. The underlying kinds already work with no
extra, so this is wrong-instruction drift, not a broken feature, but it's the
runtime channel so it misleads.

## In scope
Fix the stale `[extra]` references (flagged by the pre-ship `reviewer`):
- `src/precis/data/skills/precis-cad-help.md` — ~3× `[cad-export]`
- `src/precis/data/skills/precis-mermaid-help.md:16` — `[mermaid]` extra
- `src/precis/data/skills/precis-structure-help.md` — ~4× `precis-mcp[dft]` / `[dft]`
- `examples/cad/README.md:9-10` — `(`[cad-export]`)` next to `.stl`/`.3mf`
- `docs/conventions/thresholds.md:65` — uses `[docx]` as the "adding a new
  optional extra" example; pick an extra that's still optional (e.g. `[chem]`).

Reword each to state the kind works out-of-the-box (core dep), keeping any
genuinely-still-optional extra guidance intact.

## Explicitly NOT in scope
- Any Python source change (the in-code `ImportError` guards that name these
  extras are now dead branches — harmless, leave them or clean separately).
- The extras that stayed optional (cad-step, chem, tts, embed, paper, …).

## Acceptance criteria
- `grep -rE '\[(calc|tex|docx|plot|cad-export|dft|mermaid)\]' src/precis/data/skills examples docs/conventions` returns nothing that reads as an install instruction.
- Each touched skill still renders and its non-stale content is unchanged.

## Target + blast radius
Doc/skill files only — no handlers, verbs, routes, or workers. Zero runtime
behaviour change.
