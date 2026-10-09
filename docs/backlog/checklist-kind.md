---
status: draft
pillar: 3d-design
title: checklist kind — remainder after the pcb pre-tapeout instance shipped
prio: normal
model: sonnet
---

# checklist kind — remainder

Slices 1–3 of the original spec shipped (design session 2026-09-09;
design-of-record now lives in the package docstrings:
`src/precis/checklist/__init__.py` for the two-layer model, fingerprints
and anchors; `handlers/checklist.py` for the verbs;
`jobs/checklist_sync.py` for the shipped-file sync;
`migrations/0158_checklist_kind.sql` + `0192_checklist_pcb_tapeout.sql`
for the tables). Shipped: the five tables, put/edit/get/search, the
pcb-tapeout instance (`src/precis/data/checklists/pcb-tapeout.yaml`,
kind-default for every pcb), the three pcb tool-item bridges
(`precis.checklist.pcb`), pcb fingerprints with per-verdict `anchors`,
the `checklist_clean` auto_check evaluator, argument-thread rendering,
and the `precis-checklist-help` skill. Agent-facing surface:
`get(kind='skill', id='precis-checklist-help')`.

What is left, each independently shippable:

## In scope (remainder)

- **cad instance** (original slice 4) — seed content in
  `cad-assembly-checklist-seed-items.md`, a `cad` bridge (fingerprint +
  tool items joint with the shipped `precis.cad.printability` rules).
  Proves the kind is generic before anyone declares it so.
- **annotated schematic view** (original slice 5) — `view='schematic'`
  places datasheet strap/boot tables next to the relevant instance/pins
  via `pcb_components.part_lcsc → parts → datasheet-of`; the review aid
  for the schematic-phase judgment items.
- **`precis-tapeout-help`** — a pcb-specific orchestration skill
  (phase-by-phase, which views to run when). `precis-checklist-help`
  carries a one-pass summary today; split it out only if that section
  outgrows its place.
- **Per-item TTL** — `bom-availability-lifecycle` is marked TTL in its
  body only; a `ttl` column + "stale (expired)" status would enforce it
  (precedent: `refs.retraction_checked_at`).
- **Doctor wiring** for `checklist_sync.check_drift` (function + test
  landed; no doctor check calls it yet).
- **A clean DRC run is invisible** — `pcb_write_drc_findings` with zero
  findings writes no row, so `pcb_drc_findings_latest` (and therefore
  both `drc-clean` and the `netlist_drc_clean` evaluator) still reads
  the previous red run after a clean one. pcb-side fix: persist a run
  marker (or one `clean` sentinel row) per run.
- **`tag` on pcb** — trailing independent from the design session;
  nothing in the shipped kind depends on it.

## Explicitly NOT in scope

- Backannotation (single source of truth; nothing to annotate back to).
- In-server LLM evaluation of judgment items: the agent reading the
  status view is the evaluator; `route()` is not called by the kind.

## Acceptance criteria

Per remainder item, written when that item is specced; the kind's own
acceptance (three-valued status, append-only ledgers, sync invariants)
is covered by `tests/test_checklist.py`, `tests/test_checklist_sync.py`
and `tests/test_checklist_pcb.py`.

## Target + blast radius

`src/precis/checklist/` (a second bridge), `handlers/pcb.py`
(schematic view, tag), `src/precis/data/checklists/` (cad YAML),
`src/precis/data/skills/`.

## Open questions / decisions log

None open. 2026-10-09: original spec trimmed to this remainder on ship
of slices 1–3; the full design text is in git history of this file.
