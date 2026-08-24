---
status: ready
title: qu164903 dossier audit residuals — sourcing UI, trust guards, prod correction
prio: high
---

# qu164903 dossier audit residuals

<!-- Origin: 2026-08-24 dossier audit (the "corner saga"). The root fix
shipped as 14e78677 (periodic-symmetry canonicalization + tick-prompt tiling
rules + inline-sourcing rule). These are the three follow-on slices that
audit surfaced but that ship did not cover. Findings verified against prod
structures (translation twins st245406≡st237458, st243092≡st239974, both
energy-identical). -->

## A. Web sourcing/render slice (code, shippable)

The smartdraft UI mislabels simulation-cited paragraphs as unsourced, and
structure handles are second-class citations:

1. `src/precis_web/routes/drafts.py::provenance_state` — only paper/patent
   count as "sourced"; a paragraph citing `[stNNN]` structures gets the red
   "unsourced — cites nothing" bar. Teach it a computational-evidence class
   (`structure`, plus `calc`/`math`/`pathway`).
2. `src/precis/utils/mentions.py::LINKIFY_KINDS` — missing `structure`
   (drifted from `_REFS_BROWSABLE_KINDS` in `routes/refs.py`; realign), so
   `structure:NNN` / `[[structure:NNN]]` render literal. `[stNNN]` bracket
   handles DO link (kind-agnostic `handle_registry` path) — the bug is the
   provenance classification, not linkify.
3. `src/precis_web/linkify.py::_CHUNK_SIGIL` — no compact glyph for
   `structure`; verbose `st245406` mid-prose in compact mode.
4. `src/precis_web/routes/smartdraft.py::_cited_sources` — "Cited sources"
   rail filters `kind == "paper"`; widen or add a "Computed evidence" rail.
5. Export parity (lower prio): `src/precis/export/latex.py::_render_target`
   and `docx.py` silently DELETE non-paper handles; `_collect_raw_cites`
   (`handlers/_citations_view.py`) and `smartdraft.py::cite_integrity_ok`
   skip them too.

⚠ Coordinate first: the main worktree's purpose was "links on read path:
search-hit link count + paper/handler…" — check for overlap before touching
linkify/refs.

## B. Barrier-trust guards (code, needs small design)

The corner saga's enabler beyond dedup: the barrier pipeline emitted 0.479 eV
and 4.99 eV for the SAME structure (st239974 vs st243092), and the dossier
narrated the difference as chemistry. Guards to add near the harvest/trust
path (`compute._pathway_quality` area):

1. Same `geom_hash_c` (or energy-twin flag), wildly different barrier →
   both untrusted + note (measurement irreproducibility, re-dispatch).
2. Absurd-magnitude barrier (e.g. > 2× bond-dissociation scale, the 12–14 eV
   readings) → auto-untrust with note instead of ranking.
3. Barrier measured on an unrelaxed structure (relax "converged in 0 steps",
   like champion st211611) → flag; the current champion's 0.355 eV rests on
   an unrelaxed atop-site geometry the campaign believes is a relaxed hollow.

## C. Prod campaign correction (Reto-run, per prod-mutation-needs-user-permission)

Prepared commands, user executes (agent prepares exact CLI/SQL only):

1. qu164903 ledger note recording the audit: corner/central are translation
   twins; the "H-decoupling collapse" (st243092, 4.9926 eV) is a
   reproducibility artifact of the champion-recipe geometry; the 2×2 corner
   grid is a null experiment. Next tick then self-corrects (~12 infected
   chunks across 4 ticks: dc3140949/51, dc3162168, dc3173095/96,
   dc3176801–04, dc3176808; also dc3023253, dc3136837).
2. Untrust st243092's barrier; re-dispatch to trust-check.
3. Re-dispatch champion st211611 relax (0-step artifact) and the mis-built
   st245914 (H atop Sn instead of the champion offset).
4. The long-deferred Cd 0 eV re-check (dc3176806) can ride along.

## Acceptance

- A: dc3176803 renders sourced (green/neutral, links visible) on the dossier;
  dc3176804-style handle-less numeric chunks stay red.
- B: a re-measured twin pair with disagreeing barriers shows untrusted, not
  ranked; 12+ eV readings never rank.
- C: next qu164903 tick's dossier drops the corner narrative and cites the
  ledger note.
