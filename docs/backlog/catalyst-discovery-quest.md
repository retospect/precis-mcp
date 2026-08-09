# The catalyst-discovery quest — remaining slices

A perpetual striving ("a Pd catalyst with the lowest achievable
rate-limiting barrier for reaction R") as an autonomous quest loop that
designs catalyst models, measures their barriers, and Pareto-ranks the
designs. The big model orchestrates via tools; the compute is the referee.

**Substantially BUILT** (present-state: `src/precis/quest/` docstrings;
build detail in git history of `docs/backlog/catalyst-discovery-quest.md`):
generalized frontier over arbitrary `meta.rubric_objectives` (§7.2),
leaderboard views (§7.3), the autocatpath harvest/dispatch/co-dispatch
bridge, the NO→NH₃/Pd quest seed (`quest/catalyst_seed.py`,
`precis quest seed-catalyst`), and the `slab` fcc(111) build op (Slice 4a,
`structure/ops.py::_op_slab`, constraints serialised via
`to_extxyz(constraints=True)`). Decided: first reaction = **NO → NH₃ on
Pd(111)** (autocatpath `examples/no_to_nh3_pd.yaml`; EMT dev / MACE real).
Auto-loop stays dark (`PRECIS_QUEST_LOOP_ENABLED` off; force-stepped until
trusted).

## Remaining scope

### First light + the agentic tick tools-loop (§7.7)

Run first light (`precis quest tick <id> --compute` → co-dispatch →
harvest → `view='leaderboard'`), then build the strategic escalation: the
escalated (opus, cascade) tick step becomes an **agentic tools-loop** so
the big model sequences its own investigation, every tool call a logbook
line. Substrate exists (the `claude_agent` tools-loop dispatch). Toolbox:

```
edit_structure(parent, ops)     # deterministic ops → a new content-addressed structure
propose_sites(structure)        # (Slice 5) probe-proposed adsorption anchors
relax(structure, fidelity='ml') # struct_relax job → stability
autocatpath(structure)          # autocatpath_explore job → barrier graph
search_literature(q)            # grounding; papers serve the quest
log(entry, type)                # WORM logbook append
rewrite_dossier(text)           # living synthesis + frontier snapshot
suggest_next(quest)             # (Slice 7) Optuna acquisition → next design point
```

The cheap default tick (haiku) stays single-shot bookkeeping/harvest.
Variant generation is not delegated to a lesser model — an edit is a
deterministic op-list the big model emits; the relax repairs geometry.

### Slice 4b — more model-building ops

Adsorbate/molecule-add ops + facets beyond fcc(111). (§7.4 residue.)

### Slice 5 — adsorbate placement rigor (§7.5)

v1 (LLM anchors the active site once via the `eye` op; adapter passes an
explicit anchor `xy` to `place_fragments()` for every intermediate) folds
into the bridge. The rigor upgrade: wire autocatpath's `poses()` ensemble
(sites×tilts), precis narrowing poses geometrically first (`plane()`,
hollow/bridge/top over surface triangles, filtered by coordination) so
autocatpath scores a short list. Cost: N poses × a relax per intermediate.
Not a v1 blocker. Gotcha kept: a non-fcc111 slab has no `adsorbate_info`,
so the anchor must be an explicit `xy`/reference-atom, never a named site.

### Slice 6 — catalysis-library pull (§7.6)

Planned import (build fresh against the live `structure` kind; do NOT
reuse the stale `precis-dft` Materials-Project ingest). Two uses: seed
designs (real relaxed slabs the big model edits) and reference anchors
(known barriers/energies to calibrate the ML backend). Source TBD
(Materials Project / OC20 / curated). Rides ADR 0053's import ladder.

### Slice 7 — Optuna optimizer advisor (§7.8)

Acquisition layer over the frontier: `suggest_next(quest)` → next param
point + current Pareto set + one-line rationale. Reconstruct the study per
call (`tell` all past trials, `ask` next) — **no persistent study; the
`serves`-graph IS the study**. Multi-objective sampler (MOTPE/NSGA-II);
in-process, no GPU. The LLM stays in charge — Optuna proposes, the model
disposes.

**Do NOW (data collection):** parametrize the design so history accrues
before the advisor lands — `meta.param_space` on the quest (named knobs +
types/ranges), `meta.params` on every candidate structure stamped at
propose time, and a param → structure decoder the LLM authors per quest
(fuzzy knobs get a concrete operationalization, e.g. `roughness` = RMS
z-spread).

## Open decisions (§8)

1. Site-finder ownership (§7.5) — recommend precis-proposes /
   autocatpath-scores.
2. Seed slabs — slab-builder op (shipped) vs library import first.
3. Backend per quest — MACE (deployed) vs FAIRChem/UMA (better for
   adsorbates); MACE for first light.

## Explicitly not v1 (§9)

DFT confirmation · implicit or explicit solvation · non-(111) facets ·
multi-stage graduation · an embedding proposer · autonomous scheduling.
