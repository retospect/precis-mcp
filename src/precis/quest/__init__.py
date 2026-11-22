"""Quest layer runtime — the striving's autonomous research loop.

Design-of-record ``quest-layer`` (git-only); kind + logbook surface in
:mod:`precis.handlers.quest`. Submodules: `reweight` (priority down the
``serves`` DAG into rotation/acquisition/reading), `gaps` (health + the
exploration queue), `logbook` (WORM entries), `dossier` (the living
synthesis), `tick` (one bounded LLM step), `compute` (candidates →
``structure`` sims), `frontier` (Pareto rank), `protocol` (per-quest
measurement protocols, deduped dispatch/harvest drift logbook entries,
frontier/status protocol histograms; gr593142), `environment_basis` and
`correction_policy` (engine-free hashes/tuples), `atomcost` (element-price
table → the ``atom_cost`` economic axis), `figures` (static matplotlib
pareto/profile renderers + frozen data snapshots for draft export; CLI
``precis quest figure``), `replay` (optimisation-benchmark read of the
measured history — per-axis top-5% coverage + best-so-far/regret of the
proposer's picks vs a seeded random baseline, gr608711; CLI
``precis quest replay`` + ``get(kind='quest', view='replay')``),
`graduate` (per-candidate
milestones), `loop` (the reconciler), `catalyst_seed` (human seeding),
`explore` (tried-set summary), `rulings` (code-minted measurement rulings),
`narrative_budget` (the growth-ratchet gate,
reusable by any rolling-context rewrite), `trap_check`
(the planted-trap regression: a dormant sandbox quest carrying a
stale-engine measure, a swapped endpoint and an absurd barrier beside
one control, ticked N times and scored on the frontier gate +
ask-vs-propose leg, gr593141 — the weekly ``quest_traps``
cadence lands the score). The perpetual loop is the
``quest_tick`` coordinator job (``precis.workers.job_types.quest_tick``).
`failure_class` (gr593138) splits untrusted barrier stamps:
mechanical auto-requeues under caps, science routes to the agent.

Package-level invariants (detail lives on the named module):

- **Need citations do not create new capabilities.** ``needs.tool_key``
  strips citation decoration and legacy capped stubs (cap before strip,
  so a filed key re-keys to itself); repeats bump even parked gripes
  without merging distinct lane prefixes.
- **Operational commits advance ticks.**
- **Environment objectives preserve missing evidence.**
  ``environment_optimizer`` evaluates one stored point without solving or
  stamping: the five eV terms, physical/trust mask and bottleneck J. Rates
  and selectivity use the matched kinetics record's T for kT; no T/p
  interpolation or U-dependent kinetics is claimed. Failed masks yield
  -infinity, unknown masks/terms stay unavailable with reasons. The
  log-rate term is unavailable for zero TOF, but its raw zero still fails
  a positive rate floor, so M=0 and J=-infinity.
  The pathway plugin's ``optimum`` read supplies staircase/map/bulk evidence.
  ``environment_stamps`` writes them (finite only,
  ``computed``/``interpolated`` footing);
  ``environment_space.declares_environment_space`` opts a quest into
  env_best ranking with a labelled run-point fallback.
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
- **Seed budgets preserve full-network search.** Defaults, caps, runner margins
  and retry eligibility: ``compute._autocatpath_wall_seconds`` (gr614723).
- **Barrier assurance separates ranking from strict credit.** PASS keeps
  the strict gate; WARN needs converged, saddle-verified evidence and permits
  only one independent seed, stale engine or BEP estimates. WARN ranks with
  an approximation marker and reasons, but cannot graduate. The frontier
  retains independent PASS-only and PASS+WARN Pareto sets. Physical failures
  and missing verification remain FAIL and cannot rank, even when the
  diagnostic provisional band retains their raw values.
  The magnitude ceiling (``compute._BARRIER_ABSURD_EV``), symmetry-twin
  disagreement (``compute._TWIN_BARRIER_TOL_EV``), and kinetics validity
  (``kinetics_trusted``) remain independent guards. Diagnostic values stay
  visible through ``frontier.ProvisionalCandidate`` in tick/text/web views.
  Seeded pathway runs record ``requested_seeds`` at dispatch and accumulate
  ``executed_seeds`` only after child launch.
  Trust verdicts show both lists and flag dispatch mismatches, while the
  independent-seed gate still uses actual trust records: starting a child
  does not prove usable independent evidence. Historical launch lists stay
  unknown; neither queued jobs nor successful-only aggregates can supply them.
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
- **Lit-search is local-first, and always peeks outside.** A tick's
  ``searches`` hit the graph first; the full Semantic Scholar + acquire leg
  runs only on a miss, and an answered query still acquires a small peek
  (gr626691). Thresholds, the peek knob, what links ``serves`` and the
  logbook line: :mod:`precis.quest.search`.
- **Human-set knobs the LLM may not tune**: ``meta.rubric_composite``,
  ``meta.fidelity_levels`` (screening→neb→verify) — seed time only.
  ``meta.operating_conditions`` and ``meta.environment_space`` use the
  quest handler's validated meta-edit door. Tick payloads have no generic
  meta-patch action, so they cannot change these bounds or goals.
  ``environment_space.effective_environment_space`` resolves detached
  defaults at optimizer read time: the kinetics sweep box plus operating
  windows/points, TOF_floor 1e-3 /s and tau_s 1 s. This keeps absence from
  becoming a tick-authored policy; explicit axes override defaults individually.
- **Bulk Pourbaix gates, never ranks.** Dispatch requires human-set
  ``operating_conditions`` and content-addresses geometry + inputs; a
  successful current-input verdict is stamped as evidence. A host-phase
  result ``dissolved_everywhere`` across the quest's ``window`` (not a
  dopant ``elemental`` sub-verdict, never an ``unmatched`` point) tags the
  candidate ``ruled-out:pourbaix-dissolved`` with a ``dead-end`` logbook
  entry; a later succeeded harvest under changed operating inputs that no
  longer meets the rule lifts the tag with a ``note`` entry citing both
  bases (``pourbaix``). Every Pourbaix field (including numeric ΔG) stays
  outside Pareto ranking (``pourbaix``/``compute``/``frontier``).
- **The surface-Pourbaix map is pure reads over stored job meta.**
  ``pourbaix_map`` folds a structure's ``surface_coverage_scan`` sweeps into
  the resting-state validity map (``view='pourbaix'`` on quest + structure,
  ``/pourbaix/<handle>``): one map per footing, never blended; the bulk
  verdict is a labelled uncertain overlay, never merged (slice-2 rulings).
  It lives in core — not ``precis_pathway`` — because core handlers serve
  the view and the plugin-import boundary forbids the import
  (``tests/test_plugin_import_boundary.py``).
- **Window/Shapley measures stamp wherever the anchors exist.**
  ``window_objectives`` (SPSO slice 6) turns the resting map + staircase
  routes into measures (window area/fraction, Shapley Σφ²/IPR, ridge
  churn, the HER margin) on every candidate carrying a coverage scan
  (the paid measurement). Declaring a key in
  ``rubric_objectives`` promotes it to a Pareto axis and the logbook;
  unflagged quests stay as before. ``precis_pathway``
  loads lazily via ``importlib``: a
  missing plugin degrades to notes,
  never a crash. Anchor-scan placement is **propose-only**: ranked
  proposals go on the logbook; the dispatcher mints.
- **Engine deploys re-score.** The autocatpath content key folds an
  engine-version token, so new results never dedupe onto stale jobs;
  ``compute.redispatch_candidates``/``reset_compute`` are the CLIs.
  Evidence minted on an older token demotes to a *named* blocker —
  ``engine:stale``/``engine:unknown`` (an unstamped trusted edge) in
  ``barrier_seed_blocker``/``barrier_blocked_by``, never a silent flip —
  and self-heals: the harvest re-queues a stale-trusted
  struct_neb edge on the current engine (daily-capped,
  ``StructureHandler.enqueue_stale_neb_rerun``), while an
  autocatpath pathway re-measures through the restale lane —
  for opted-in frontier and top provisional candidates.
  ``fidelity_restale_cap=0`` holds automatic restale, promotion, repair,
  wrongful-kill reconciliation and tick reruns (gr543908). Operators can
  still use ``request_rerun`` and ``redispatch_candidates``. Mechanical
  stamps and classified infra/timeout failures retry on the pinned
  engine with either hold setting (gr593138), subject to a cooldown,
  ``fidelity_mechanical_retry_cap``, and
  ``fidelity_mechanical_gpu_h_budget`` at 1.5 GPU-h per seed. A fresh science
  verdict vetoes failed-job redispatch, including amnesties, and routes
  the blocker to the quest agent. ``view='trust'`` matches harvest staleness.
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
