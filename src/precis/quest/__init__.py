"""Quest layer runtime — the striving's autonomous research loop.

Design-of-record ``quest-layer`` (git-only); kind + logbook surface in
:mod:`precis.handlers.quest`. Submodules: `reweight` (priority down the
``serves`` DAG into rotation/acquisition/reading), `gaps` (health + the
exploration queue), `logbook` (WORM entries), `dossier` (the living
synthesis), `tick` (one bounded LLM step), `compute` (candidates →
``structure`` sims), `frontier` (Pareto rank), `graduate` (per-candidate
milestones), `loop` (the reconciler), `catalyst_seed` (human seeding),
`explore` (tried-set summary), `narrative_budget` (the growth-ratchet gate,
owner-agnostic — reusable by any rolling-context rewrite). The perpetual loop itself is the
``quest_tick`` coordinator job (``precis.workers.job_types.quest_tick``).

Package-level invariants (each enforced where named):

- **The discovery agent owns all chemistry.** ``catalyst_seed.PARAM_SPACE``
  is coverage-count + buildability only, never a chemistry menu; code never
  fabricates a dispatch or picks chemistry (``tick``).
- **Infra failure is never a physical verdict.** A failed relax or
  autocatpath job retries once then gripes — it is never ``ruled-out:``
  (``compute``; dossier-owned-by-process).
- **Untrusted barriers don't rank — but they stay visible.** A pathway with
  NEB-not-converged / adsorbate-detached warnings is excluded from the
  confirmed frontier and can never graduate (``compute._pathway_quality`` →
  ``frontier`` → ``graduate``), but its measured values surface as a
  **provisional** band (``frontier.ProvisionalCandidate`` — merged measures,
  untrusted keys + reasons named, own Pareto rank) in the tick prompt, text
  view, and web scatter, so the loop is never blind to its own measurements.
- **The dossier can't bloat or lose its trail.** The pinned ledger is a
  nested attempt tree mutated only via explicit ops
  (``dossier.add_attempt``/``mark_attempt`` — exact-text addressed,
  whitespace-normalized against node forgery); ``add_attempt`` **upserts**
  (whole-ledger near-dup Jaccard match advances the existing node's status,
  never appends a twin or regresses; a conflicting element signature —
  ``dossier._elements_conflict``, Rh vs Ru — vetoes the match, since the
  ≥4-char token floor makes two-letter dopant symbols invisible to Jaccard;
  ``dedup_ledger`` / ``precis quest dossier-dedup`` retrofits old ledgers). The narrative is stored one
  paragraph per unpinned chunk (retire + re-insert wholesale each rewrite,
  so per-thought embeddings recompute), and the rewrite passes
  ``narrative_budget.narrative_growth_gate`` (growth beyond 15%+50 words
  needs same-tick progress evidence; ~2500-word ceiling tripwire; one
  compress-retry then keep-previous + logbook entry). Word counts land in
  logbook meta so thresholds are tuned from data.
- **Loop existence is reconciled, not allocated.** ``loop`` guarantees one
  live coordinator per active quest (idempotent re-mint, reboot-orphan reap,
  failed-rest *and* dry-rest backoff — a ``meta.rest_reason == "dry"``
  success rest cools on the same exponential window, and once the quest's
  ``consecutive_dry_rests`` counter reaches its threshold the reconciler
  skips it for an escalation window + raises an operator alert,
  auto-recovering (gr170252, see ``loop.py``'s module docstring); ``allocator``
  backs only the manual ``precis quest run`` one-shot.
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
