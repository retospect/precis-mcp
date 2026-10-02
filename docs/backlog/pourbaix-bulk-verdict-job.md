---
status: draft
title: a pourbaix_bulk job returns a recomputable bulk Pourbaix verdict for a candidate's host phase over a U/pH window
pillar: 3d-design
prio: high
model: opus
---

# A `pourbaix_bulk` job returns a recomputable bulk Pourbaix verdict over a U/pH window

Part A of the bulk Pourbaix gate. Part B (quest integration: dispatch,
stamp, rule-out, leaderboard) is `pourbaix-quest-gate.md`, blocked by this
item. Design-reviewed 2026-10-02 (orchestrator, design note §13, changes
S1–S5) and readiness-reviewed the same day (6 blockers, resolved below).

## Motivation / why

Reto, 2026-10-02: catalyst quests (first qu202468, non-Pd Cu-based
NO→NH₃: Cu₃P(001) st205850, CuRh₁, Ti–Cu, …) rank on reaction energetics
only. A candidate whose bulk dissolves or turns into another phase at the
electrolysis conditions cannot be the catalyst that was modelled.

`src/precis_dft/jobs/pourbaix.py` is unusable for this: a simplified
μ(U, pH) without ion-activity corrections, no job type, no caller (only
`tests/precis_dft/test_pourbaix.py` imports it), and nothing writes the
`meta.derived.pourbaix` block its `from_materials()` reads. This item
uses `pymatgen.analysis.pourbaix_diagram.PourbaixDiagram` with Materials
Project Pourbaix entries (ion-corrected, Persson et al. 2012).

A bulk verdict is a **necessary condition, not a sufficient one**: a
bulk-stable phase can still restructure at the surface under bias. The
verdict text an agent reads says so (surface Pourbaix is a later slice,
`qu164903-campaign.md` §"Surface Pourbaix view").

## In scope

1. **Host composition** (readiness blocker 1). From the candidate's
   structure spec ops when present: host = the slab/bulk op's elements +
   `substitute` targets + metal `add_atom`s; adsorbates = `add_adsorbate`
   ops. Fallback (no ops): the materialised scene's z-layers
   (`precis.structure.invariants._layers`): every atom in the slab's layer
   stack is host; above-top-layer atoms of H, C, N, O are adsorbates,
   other elements are host dopants. The rule and which path ran are
   recorded in the result.
2. **Dopant vs host phase** (blocker 2, S3). An element with host atom
   fraction < 0.10 that entered via `substitute`/`add_atom` (or, on the
   scene path, sits only above the top layer) is a **dopant**. The
   **host phase** is the remaining composition.
3. **Host phase → MP phase** (blocker 2). Slabs are not stoichiometric
   (a Cu₃P(001) slab is rarely 3:1). Match the host phase to the MP
   solid in its chemsys with the nearest fractional composition (L1
   distance over non-O/H element fractions), choosing the lowest
   `energy_per_atom` among same-formula solids. Distance > 0.15 →
   verdict `unmatched` (flag, never a rule-out). The matched formula and
   distance are recorded. `comp_dict` is built from the matched entry's
   composition, so `get_decomposition_energy` cannot raise on a mismatch.
4. **Dopants are not assessed in the alloy** (S3). The verdict is the
   host-phase verdict. Each dopant gets a separate elemental verdict,
   recorded as information under `pourbaix:dopant-unassessed`. It never
   drives a rule-out.
5. **Classification over the whole domain** (S1). At a (U, pH) point,
   `get_stable_entry(pH, V_SHE)` returns a (Multi)Entry; classify over
   its `entry_list`, per host element:
   - every host element in an aqueous ion → `dissolved`;
   - some host elements in ions, some in solids → `leached` (record the
     surviving solid(s) and the leached ion(s); e.g. Cu₃P → Cu(s) + a
     phosphate ion);
   - all host elements in solids: the matched phase itself with
     ΔG_pbx ≤ `stability_tol` → `stable`; a different solid containing
     O → `oxidised`; a different solid with no O → `transformed`
     (replaces the unoperational "reduced": a hydride, a lower-valence
     solid, or a different stoichiometry; the solid is named).
   ΔG_pbx from `get_decomposition_energy(entry, pH, V_SHE)` (eV/atom).
   `stability_tol` default 0.1 eV/atom, a job parameter (the GGA error is
   of the same order; S2 makes the number recomputable rather than final).
6. **A window, not a point** (S4). Input: an operating point
   (U_RHE, pH) and an optional window (U_RHE range, pH range). The job
   evaluates the point and a grid over the window (default 5×5, a
   parameter) and returns the point verdict, the per-grid-point verdicts,
   and `worst_in_window` (order: dissolved > leached > transformed >
   oxidised > unmatched > stable) plus `dissolved_everywhere` (bool).
   V_SHE = U_RHE − PREFAC·pH, with PREFAC imported from pymatgen
   (`pymatgen.analysis.pourbaix_diagram.PREFAC`, 0.0591) so the conversion
   and the diagram agree.
7. **The result carries its inputs** (S2). Every result stores ΔG_pbx,
   the domain's entry names, (U_RHE, pH, ion_conc_M, window), the MP
   database version, the matched phase and distance, the composition path
   (ops/scene) and `stability_tol`. Part B compares these to decide when
   to re-evaluate. Data source and licence line: "Materials Project
   <version>, CC-BY 4.0".
8. **Known limits stated in the result text.** MP Pourbaix entries hold
   solids and aqueous ions, not gases; a decomposition that would evolve
   PH₃, H₂ or NH₃ is not represented. Plus the necessary-not-sufficient
   sentence above.
9. **Placement** (blocker 5). Pure engine
   `src/precis_dft/pourbaix_bulk.py` (pymatgen + mp_api only; imports
   nothing from precis, per the `precis_dft` subtree rule):
   `verdict(entries, host, dopants, point, window, tol) -> dict`.
   Job type `src/precis/workers/job_types/pourbaix_bulk.py`
   (`JobTypeSpec`, `claude_inproc` dispatch like `news_poll`, so it runs in
   `com.precis.worker`). `PARAMS_SCHEMA`: `candidate_ref`, `point`,
   `window`, `ion_conc_M`, `stability_tol`, `grid`. `REQUIRES` a
   `has_pourbaix` capability, so only a host with the extra claims it.
   The verdict is written to the job's meta (`ctx.set_meta(verdict=…)`),
   and Part B's harvest reads it from there, the same pattern as the
   autocatpath harvest.
10. **MP fetch and cache.** `mp_api.client.MPRester(api_key)
    .get_pourbaix_entries(chemsys)` once per chemsys per job. No
    `material`-kind cache (that kind is a CRC property store; readiness
    advisory). The entries are cached as a JSON artifact keyed by
    (chemsys, MP version) in the job's own output, and a later job reuses
    the newest successful job's artifact for the same key
    (`_find_job_by_idem_key`-style lookup). Size bound: a chemsys with
    > 3 non-O/H elements is refused (`failure_class="input"`) in v1, which
    caps the combinatorial `PourbaixDiagram` build. `nproc=1`.
11. **Secret** (blocker 4, S5). `precis.secrets.get_secret
    ("PRECIS_MP_API_KEY")`, which reads the process env first, then the DB
    vault (ADR-0055). It is provisioned in the vault, and a row is added to
    `docs/reference/config-variables.md`'s vault table. A missing key →
    `ctx.record_failure` with `failure_class="config"` and no verdict. It is
    never a silent "stable".
12. **Dependencies** (S5). New extra `[pourbaix]` = `pymatgen` (same pin
    as `[estimate]`) + `mp-api`; `uv lock` regenerated; a mypy override for
    `mp_api`. The extra is added to the `precis_worker` deploy role's
    extras so `com.precis.worker` on the worker host has it
    (`deploy-extras-gap`). The CI extras set includes it so the tests run
    there. The dev image gets it at the next announced rebuild; until then
    local runs use `UV_WITH="--with numba --with pymatgen --with mp-api"`.
13. **Tests hard-require the deps** (no `importorskip`: a skipped test
    here is a vacuous green). The engine is tested on hand-built entries
    with no network: `PourbaixEntry(PDEntry(...))` / `IonEntry(Ion
    .from_formula(...), ΔGf)` from tabulated Cu, Cu₂O, CuO, Cu²⁺, HCuO₂⁻
    and Cu₃P / H₂PO₄⁻ / HPO₄²⁻ / PO₄³⁻ formation energies, with values and
    sources in the test file. One opt-in live test (`-m mp_live`, needs the
    key) re-checks the same Cu cases against MP and records the MP version.
14. **Retire** `src/precis_dft/jobs/pourbaix.py` + its test in the same
    change.

## Explicitly NOT in scope

- Anything quest-side (dispatch, harvest, tags, leaderboard, prompt) —
  `pourbaix-quest-gate.md`.
- Surface Pourbaix / coverage; DFT- or MLIP-computed formation energies;
  dissolution kinetics; gas-evolving decompositions.
- Dopant-in-alloy stability (needs alloy energies MP does not hold for a
  dilute site).

## Acceptance criteria

- Hand-built-entry tests: Cu at pH 7, U_RHE −0.2 V → `stable`; Cu at
  pH 1, +0.6 V → `dissolved` (Cu²⁺); Cu at pH 13, +0.6 V → `oxidised`;
  Cu₃P at a cathodic point where phosphate is the stable P species →
  `leached` naming Cu(s) and the phosphate ion; a window spanning a
  dissolved and a stable region → point verdict at the point,
  `worst_in_window = dissolved`, `dissolved_everywhere = false`.
- A host-phase mismatch > 0.15 → `unmatched`; a 1-atom Rh dopant in Cu →
  host verdict for Cu plus a `pourbaix:dopant-unassessed` entry for Rh.
- The result dict carries every input listed in item 7.
- A job with no key fails with `failure_class="config"` and no verdict.
- `uv run mypy src tests` is clean with the new override; the old module
  and its test are gone.

## Target + blast radius

New: `src/precis_dft/pourbaix_bulk.py`,
`src/precis/workers/job_types/pourbaix_bulk.py` (+ registry entry),
tests. Changed: `pyproject.toml` (extra, mypy override), `uv.lock`,
`deploy/roles/precis_worker` extras, the CI extras list,
`docs/reference/config-variables.md`. Removed: `src/precis_dft/jobs/pourbaix.py`
and its test. No migration. No prod writes: nothing dispatches the job
until Part B.

## Open questions / decisions log

- **Reto:** supply a Materials Project API key for the vault (review-queue
  `catalysis-selectivity-4`).
- **Decided, design review S1–S5 (2026-10-02):** domain-wide
  classification with `leached`; recomputable verdict carrying its
  inputs; no rule-out from the elemental fallback; window evaluation;
  dependency in an extra, the worker role and the deploy.
- **S5 vs readiness blocker 4:** the review said "API key in the deploy
  template"; repo convention (ADR-0055) is the vault via `get_secret`,
  which also honours a process env var. Built on `get_secret`, provisioned
  in the vault; an env-var fallback needs no code change.
- **Readiness review (2026-10-02):** blockers 1, 2, 3, 4, 5 resolved in
  items 1–3, 13, 11, 9; blocker 6 (operating point) belongs to Part B.
  Advisories folded in: PREFAC from pymatgen; "reduced" replaced by
  `transformed`; MultiEntry classification; lowest `energy_per_atom` pick;
  `comp_dict` from the matched entry; chemsys cap; no `material` cache;
  hard-required test deps; mypy override.
