"""Quest layer runtime — the striving's autonomous research loop.

Design-of-record ``quest-layer`` (git-only); kind + logbook surface in
:mod:`precis.handlers.quest`. Submodules: `reweight` (priority down the
``serves`` DAG into rotation/acquisition/reading), `gaps` (health + the
exploration queue), `logbook` (WORM entries), `dossier` (the living
synthesis), `tick` (one bounded LLM step), `compute` (candidates →
``structure`` sims), `frontier` (Pareto rank), `atomcost` (static
element-price table → the ``atom_cost`` economic axis), `figures` (static matplotlib
pareto/profile renderers + frozen data snapshots for draft export; CLI
``precis quest figure``), `graduate` (per-candidate
milestones), `loop` (the reconciler), `catalyst_seed` (human seeding),
`explore` (tried-set summary), `rulings` (code-minted measurement rulings),
`narrative_budget` (the growth-ratchet gate,
owner-agnostic — reusable by any rolling-context rewrite). The perpetual loop itself is the
``quest_tick`` coordinator job (``precis.workers.job_types.quest_tick``).

Package-level invariants (detail lives on the named module):

- **The discovery agent owns all chemistry.** ``catalyst_seed.PARAM_SPACE``
  is coverage-count + buildability only, never a chemistry menu; code never
  fabricates a dispatch or picks chemistry (``tick``).
- **A periodic cell tiles — symmetry twins are one candidate.** Candidate
  scenes are stored canonicalized (``StructureHandler.put(normalize=True)``)
  and stamped ``geom_hash_c`` (:mod:`precis.structure.canonical`); a
  proposal matching an existing candidate's canonical hash is soft-deleted
  with a logbook note, never dispatched (``compute``); the frontier lazily
  backfills the stamp on pre-cutover candidates and flags energy-degenerate
  same-composition pairs (``frontier``). The tick prompt states the tiling
  rules (``tick._reaction_context``).
- **Infra failure is never a physical verdict.** A failed relax or
  autocatpath job retries once then gripes — it is never ``ruled-out:``
  (``compute``; dossier-owned-by-process).
- **Untrusted measures don't rank — but they stay visible.** A pathway with
  NEB-not-converged / adsorbate-detached warnings, a nonphysical barrier
  (> ``compute._BARRIER_ABSURD_EV``), a symmetry-twin pair whose barriers
  disagree (same ``geom_hash_c``+tier, Δ > ``compute._TWIN_BARRIER_TOL_EV``
  — both sides untrusted), or a kinetics solve whose
  guard bracket disagrees / TOF is non-finite (``kinetics_trusted``), is
  excluded from the
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
  logbook meta so thresholds are tuned from data. The per-hypothesis
  **dialectic** (support / counter / discriminating experiment, one pinned
  block per live hypothesis finding) is likewise op-mutated, never rewritten
  (``dossier.apply_dialectic_op`` via the tick's ``dialectic_ops`` key), and
  mints ``supports``/``contradicts`` evidence edges at apply time —
  quest-dossier-dialectic §Mechanism. Its sims→findings anchor is
  ``rulings`` (``mint_measurement_rulings``, a code-only pre-LLM tick pass):
  a trusted measurement matching an experiment entry's pre-registered
  ``[st…]`` structure mints a templated **measurement-ruling finding** (no
  LLM authorship, no STATUS tag — internal only, never nanopub evidence)
  plus a ``tests`` edge (measuring pathway → hypothesis, migration 0142);
  the next tick interprets it via support/counter/settle.
- **Loop existence is reconciled, not allocated.** ``loop`` guarantees one
  live coordinator per active quest (idempotent re-mint, reboot-orphan
  reap, failed/dry-rest backoff + escalation; see :mod:`precis.quest.loop`);
  ``allocator`` backs only the manual ``precis quest run`` one-shot.
- **Human-set knobs the LLM may not tune**: ``meta.rubric_composite``,
  ``meta.tier_ladder`` (screening→neb→verify) — seed time only.
- **Engine deploys re-score.** The autocatpath content key folds an
  engine-version token, so new results never dedupe onto stale jobs;
  ``compute.redispatch_candidates``/``reset_compute`` are the CLIs.
- **One proposal in flight (WIP=1).** ``tick.max_proposals_per_tick``
  (``PRECIS_QUEST_MAX_PROPOSALS``, default 1) caps dispatch/tick; extras
  stay ``hypothesis`` leads until the in-flight one's sims land.
- **Bad energies are part of the score** (catpath >= 0.6.0 scorecard):
  ``selectivity_margin``/``poison_margin`` (max), ``trap_margin``
  (diagnostic) ride the barrier's trust gate; the default rubric ranks on
  barrier/energy/selectivity_margin/poison_margin, so
  ``reaction_config.poisons`` must screen ≥1 species or ``poison_margin``
  is an empty-frontier trap.
"""
