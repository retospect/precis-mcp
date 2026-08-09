"""Quest layer runtime — the striving's autonomous research loop.

Design-of-record ``quest-layer`` (git-only); kind + logbook surface in
:mod:`precis.handlers.quest`. Submodules: `reweight` (priority down the
``serves`` DAG into rotation/acquisition/reading), `gaps` (health + the
exploration queue), `logbook` (WORM entries), `dossier` (the living
synthesis), `tick` (one bounded LLM step), `compute` (candidates →
``structure`` sims), `frontier` (Pareto rank), `graduate` (per-candidate
milestones), `loop` (the reconciler), `catalyst_seed` (human seeding),
`explore` (tried-set summary). The perpetual loop itself is the
``quest_tick`` coordinator job (``precis.workers.job_types.quest_tick``).

Package-level invariants (each enforced where named):

- **The discovery agent owns all chemistry.** ``catalyst_seed.PARAM_SPACE``
  is coverage-count + buildability only, never a chemistry menu; code never
  fabricates a dispatch or picks chemistry (``tick``).
- **Infra failure is never a physical verdict.** A failed relax or
  autocatpath job retries once then gripes — it is never ``ruled-out:``
  (``compute``; dossier-owned-by-process).
- **Untrusted barriers don't rank.** A pathway with NEB-not-converged /
  adsorbate-detached warnings is excluded from the frontier and can never
  graduate (``compute._pathway_quality`` → ``frontier`` → ``graduate``).
- **Loop existence is reconciled, not allocated.** ``loop`` guarantees one
  live coordinator per active quest (idempotent re-mint, reboot-orphan reap,
  failed-rest backoff); ``allocator`` backs only the manual
  ``precis quest run`` one-shot.
- **Human-set knobs the LLM may not tune**: ``meta.rubric_composite`` and
  ``meta.tier_ladder`` (screening→neb→verify) are written at seed time only.
- **Engine deploys re-score.** The autocatpath content key folds an
  engine-version token so new engine results never dedupe onto stale jobs;
  ``compute.redispatch_candidates`` / ``reset_compute`` are the CLIs.
- **One proposal in flight (WIP=1).** ``tick.max_proposals_per_tick``
  (``PRECIS_QUEST_MAX_PROPOSALS``, default 1) caps materialise/dispatch per
  tick; the coordinator's per-quest backpressure holds the next tick until
  that proposal's sims land. Extra proposals stay ``hypothesis`` leads.
- **The bad energies are part of the score** (catpath >= 0.6.0 engine
  scorecard): ``selectivity_margin`` (max), ``poison_margin`` (max),
  ``trap_margin`` (harvested diagnostic) ride the barrier's harvest + trust
  gate (``compute._AUTOCATPATH_SELECTIVITY_KEYS``); the default rubric ranks
  on barrier/energy/selectivity_margin/poison_margin, and
  ``reaction_config.poisons`` must screen at least one species or
  ``poison_margin`` is an objective nothing produces (empty-frontier trap).
"""
