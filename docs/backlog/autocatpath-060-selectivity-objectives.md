# autocatpath 0.6.0 — selectivity + poisoning objectives (qu164903 restart)

Supersedes `quest-backpressure-sim-types.md` (code fix shipped b0eb2c03;
its "operational residual" wrongly claimed the 164903 pause was applied —
prod still shows STATUS:active, the wipe is stage 1 below) and
`quest-job-sequencing.md` (the WIP question is decided: ONE proposal in
flight — `tick.max_proposals_per_tick`, default 1, shipped with this item).

Operator directive (2026-08-08): adopt the new engine variant and widen the
cost function — "relative barrier for side product" + "poisoning resistance"
("use the bad energies as part of the score"); feed side-product/poison
context to the literature reviewer; one proposal in flight at a time; reset
quest 164903's stale-engine conclusions.

## Engine facts (catpath 0.6.0, released to PyPI 2026-08-08 @ 0958c7e)

0.6.0 on top of 0.5.2's pure `analyze()` (all 0.5.2 sections survive
unchanged):

- `results.json.score` — always-on four-axis scorecard: `activity`
  (span, low good), `selectivity` (worst branch-point margin: side climb −
  main climb at the same fork), `poison` (worst `delta_vs_substrate`),
  `trap` (span − worst off-route escape) — margins high-good — plus
  `limiting_factor` + one-line `worst_problem`. Precis lifts the two
  strings as candidate context; the ranking scalars stay the 0.5.2
  span-based lifts (see residuals).
- `mlip.dtype: mixed` — relaxations descend float32, finish float64; NEB
  always float64. `mlip.cueq: auto` — cuEquivariance CUDA kernels when
  `cuequivariance-torch` is installed (precis's `catalyst-gpu` extra now
  ships it, Linux-only), silent fallback otherwise.
- `search.pose_count` REMOVED (was never consumed; pose diversity =
  `search.seeds` length × `bind_reseat_attempts`). `Config.from_dict`
  pops a leftover key, so old configs still parse — but the retool below
  drops it from prod meta anyway.

- `results.json.traps` (always on): per-state cheapest escape vs the best
  route's span; `trap: true` = self-poisoning resting state. Required product
  leaves never flagged.
- `results.json.poisons` (`Config.poisons: ["CO"]`): per-species clean-slab
  `e_ads`, `delta_vs_substrate` (< 0 → poison outcompetes substrate),
  verdict `blocks|competitive|weak`. Measured in `run_one_seed`'s ledger-refs
  block → rides partials → `aggregate_partials` folds.
- Side-product chemistry: NH2OH branch, H-assisted scission, N–N coupling to
  N2O*/N2+O; `viz._select_paths` ranks main vs side paths by energetic span.
- `template: None` now defaults the ammonia network to **coadsorbed**
  (parked is explicit opt-in). Side-product rejoin + N–N coupling need
  coadsorbed.

## Precis changes (this worktree)

1. `pyproject.toml`: `autocatpath>=0.6.0` (catalyst + catalyst-gpu extras;
   catalyst-gpu also ships `cuequivariance-torch` on Linux);
   `compute._AUTOCATPATH_CACHE_EPOCH` → "0.6.0".
2. `precis_pathway/runner.py::_summary`: mirror 0.5.1 `write_outputs` —
   add `traps` (via `autocatpath.viz.trap_report`), `poisons`
   (verdict via the side-margin rule), and a `selectivity` section
   (best main span vs best side span + worst side leaf, via
   `viz._select_paths`). NOTE: the CHE electro pass-through in
   `_dispatch_common` was found DEAD on this path (`_summary` never emitted
   U_L/…); fixing that is a separate residual, not this change.
3. `precis_pathway/_dispatch_common.py::summarize`: lift scalars
   `side_span_margin` (best_side_span − best_main_span, eV, sense max),
   `trap_depth` (max escape−span_ref over non-required states, eV, sense
   min), `poison_margin` (min delta_vs_substrate, eV, sense max) + context
   strings `side_worst`, `trap_worst`, `poison_verdicts` onto job meta.
4. `quest/compute.py`: lift the three scalars in
   `_autocatpath_measures_from_job` (`_AUTOCATPATH_SELECTIVITY_KEYS`);
   context strings → candidate flags; extend `_AUTOCATPATH_MEASURE_KEYS`
   (reset nulling); pass `poisons`/`diagram`/`template` through from
   `reaction_config` in `dispatch_autocatpath`'s engine config.
5. `quest/frontier.py`: extend the untrusted-exclusion list with the three
   new scalars; add the context strings to `_META_NON_MEASURE`.
6. `quest/catalyst_seed.py::RUBRIC_OBJECTIVES` += `side_span_margin` (max),
   `poison_margin` (max). `trap_depth` stays a harvested diagnostic, not a
   default Pareto axis (5-axis domination too weak; per-quest opt-in via
   `rubric_objectives`).
7. `quest/tick.py`: (a) selectivity/poisoning paragraph in
   `_reaction_context` — instructs the model to lit-search "most undesired
   side product" / "most likely poison" for the chemistry when the dossier
   lacks them, and to state relevance vs computed traps/poisons; (b)
   proposals capped to ONE per tick (`PRECIS_QUEST_MAX_PROPOSALS`, default
   1) + prompt text 0–1. With the per-quest backpressure gate this yields
   WIP=1 proposals end-to-end.
8. Tests + docs (package docstrings, this file, quest-job-sequencing.md,
   quest-backpressure-sim-types.md).

## Prod steps (USER-RUN — agent must not mutate prod)

Script: `restart_quest_164903.py` (session scratchpad — copy to melchior),
dry-run by default, `APPLY=1` to mutate, DSN from the com.precis.web plist.

1. `STAGE=wipe` — run NOW (deploy-independent): quest → dormant, cancel
   all STATUS:queued `autocatpath_seed` jobs (~117, stale engine + config;
   seed todos kept as the dedup guard), then the printed
   `precis jobs kill <id>` for the running one. Prod check 2026-08-08:
   the earlier pause was never applied — the quest is still active.
2. Ship+deploy precis (this worktree) — the ansible role reinstalls
   autocatpath (pin >= 0.6.0). No deploy role exports
   `PRECIS_AUTOCATPATH_VERSION` (see residuals), so the epoch bump in
   step 1 is what re-keys every content key.
3. `STAGE=retool` — quest meta (`rubric_objectives` += the two new axes;
   `reaction_config.poisons = ["CO"]`; DROP `search.pose_count` (removed
   in 0.6.0 — the engine tolerates it, but leave prod meta clean);
   `mlip.dtype = "mixed"` (the 0.6.0 speed lever); `template` stays
   absent → coadsorbed default), then `compute.reset_compute` +
   re-activate.
   **Deliberately NO `redispatch_candidates`** — all ~21 candidates at
   ~3 h/seed would re-flood the serial GPU lane for days; instead the
   WIP=1 loop re-evaluates one at a time (a duplicate proposal still
   dispatches fresh under the new engine token).

## Residuals

- **0.6.0 scorecard scalars not adopted as ranking axes.** The rubric's
  `side_span_margin` stays the 0.5.2 route-span definition; 0.6.0's
  `score.selectivity.margin_eV` (branch-point climb margin — a sharper
  local measure) and `score.trap.margin_eV` (sign-flipped vs our
  `trap_depth`, and excludes on-route states) are candidates to replace
  them. Swapping means renaming keys in prod quest meta + the reset
  nulling list — a deliberate follow-up, not a drive-by.

- **`PRECIS_AUTOCATPATH_VERSION` is exported by no deploy role** (verified
  2026-08-08 — repo-wide grep hits only compute.py). The engine-token env
  override is dead wiring; today the code-constant
  `compute._AUTOCATPATH_CACHE_EPOCH` must be bumped alongside every
  autocatpath pin bump or stale jobs dedup-pin the new engine (the
  qu164903 trap, memory `catpath-barrier-trust`). Durable fix: template
  the installed version into the worker env in the `autocatpath` role
  (memory `env-config-vs-cli-arg-gap` is the same failure class), or
  drop the env and derive the token from
  `importlib.metadata.version("autocatpath")` at dispatch time.
- **CHE electro pass-through was dead on the fan-out path** until this
  item's `analyze()` delegation — if any prod pathway claims U_L/… values
  from before this deploy, they came from the legacy monolith path only.
- **Tier ladder semantics shift under 0.5.x's coadsorbed default**
  (`compute._apply_tier_config`): the `neb` rung leaves `template` absent,
  which now resolves to coadsorbed — semantically identical to the
  `verify` rung (only the content keys differ, absent vs explicit). The
  ladder degrades to screening(parked) → neb(=verify) → verify(re-run).
  Decide: pin `template: "parked"` on the neb rung to preserve the cheap
  middle rung, or collapse verify. Irrelevant for qu164903 (pre-ladder,
  ladder off → straight-to-neb identity config), so not blocking this
  ship; matters for the next `seed_catalyst_quest` (default
  `tier_ladder=True`).

## Dossier decision (operator asked: compact vs wipe-and-restart)

Recommendation: **reset, don't delete** — `reset_compute` (no
`keep_dossier`) is purpose-built for "engine change invalidates the
conclusions": dossier → stub (next tick regenerates from trusted data),
barrier measures nulled, stale `ruled-out:*`/graduation tags dropped,
while candidates, linked literature, logbook, and ledger survive. A full
quest wipe would discard harvested papers + candidate designs that remain
valid under the new objective. Caveat logged as a `decision` entry: ledger
rule-outs decided pre-0.5.1 predate the selectivity/poisoning axes.
