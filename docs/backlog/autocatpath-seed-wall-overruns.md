# autocatpath_seed genuine overruns — reduce cost, checkpoint

Seeds overrun the runner's ~3 h `ssh_node` wall by a wide margin, not a
sliver: on the prod DB only 4 seeds have ever completed (all 2026-08-03),
taking **8.5–13 h each**, and a seed still running on 2026-08-09 (`auj_2t19`,
old 0.4.x engine) was at **~10.5 h wall / ~289 CPU-h and no `result.json`**.
So the binding constraint is per-seed compute cost, 3–4× over budget — NOT
`fmax`: at 0.1 eV/Å (`neb_fmax` 0.15) it is already 2× looser than the
ASE/OC20 norm; do NOT tighten it. The 5400 s compute budget fired once, also
not the binding constraint.

Status 2026-08-09:

- **autocatpath 0.11 INTEGRATED into precis + shipped (this rev — supersedes
  the 0.8/0.9 "pending integration" bullets below).** The whole fast-screening
  NEB stack is now wired onto the **neb tier** overlay in `_apply_tier_config`
  (`quest/compute.py`), each an explicit-caller-overridable default:
  `neb_schedule=best_first` (0.7, fewer edges NEB'd) + `neb_optimizer=neb-ode`
  (0.8, ~5× fewer evals/edge — benchmarked below) + `neb_batched=true` (0.9
  MACE-dtype fix + 0.11 tether-guard fix, one MLIP forward/step across interior
  images → GPU saturation). `neb-ode` + `neb_batched` compose on the single-band
  `neb_barrier` path (verified in catpath: `_neb_attempt` wraps a `BatchedNEB`
  with the ode optimizer, no guard); the inter-band `neb_pool_size` is the one
  that rejects ode, and the pipeline doesn't feed it yet, so it's untouched.
  Pin bumped `autocatpath>=0.7`→`>=0.11.0` (`pyproject.toml` `catalyst` +
  `catalyst-gpu`) — the pin floor derives the engine token, so the bump re-keys
  every idem key → the retained candidates re-dispatch clean on 0.11 (the
  "start again"). `_AUTOCATPATH_CACHE_EPOCH` synced `0.7.0`→`0.11.0` (fallback
  only; derivation wins on any dispatch host). Verify tier stays exhaustive +
  default optimizer (authoritative final pass carries no honest-absence caveat).
  Spark GPU venv already on 0.11.0 (out-of-band, 2026-08-09); playbook 44
  formalizes the pin on deploy. **Objective flipped** with this ship (log entry
  2001): quest 164903 `rubric_objectives` → Pareto `span_at_Uopt·U_L_abs·
  energy·P_side` (all min) — the harvest already lifts all four
  (`_AUTOCATPATH_ELECTRO_KEYS` + derived `U_L_abs`), so the frontier scores them
  from the first 0.11 result. **OPEN (post-deploy confirmation):** watch the
  first fresh 0.11 seed — does it land `result.json` UNDER the ~3 h wall (the
  combined neb-ode + batched + best_first cost cut), and does `neb-ode`
  converge on the hard `[NO->N+O]` dissociation edge best_first can't prune?

- **autocatpath 0.8.0 shipped the `search.neb_optimizer` knob + a regression
  benchmark — benchmarked HERE on our chemistry, verdict: use `neb-ode`.**
  0.8.0 (`27d49e3`) makes the NEB band relaxer config-tunable
  (`bfgs`|`lbfgs`|`fire`|`neb-ode`) with `tests/test_neb_optimizer.py` proving
  cross-optimizer barrier agreement + force-eval counts. Ran catpath's own
  `neb_barrier` under **MACE-medium/float64** on a **Pd(111)+N adatom hop**
  (representative of the no_to_nh3_pd surface + N* fragment), all four
  optimizers, two regimes:
  - **Screening `neb_fmax` 0.1, 5 images (the production regime):** `bfgs` =
    `lbfgs` 0.259 eV @ **861** evals; **`neb-ode` 0.273 eV @ 161 evals — a
    5.3× MLIP-cost cut at the same barrier** (within the 0.05 eV gate), and it
    converged. `fire` is a **trap**: non-converged, barrier drifted +0.30 eV,
    5× *more* evals (4221) — the EMT "most robust" hint does NOT transfer to a
    stiff MLIP PES. `lbfgs` is a **no-op** vs `bfgs` (identical 861 evals — it
    saves memory, not evals). So the earlier "swap BFGS→LBFGS" instinct was
    wrong; **neb-ode is the lever**, and it's a pure config flip (no engine
    change beyond the 0.8 knob).
  - **Tight `neb_fmax` 0.05, 7 images:** NEITHER `bfgs` (5427 evals) nor
    `neb-ode` (1467) converged within `max_steps`=200; their non-converged
    barriers diverged 0.57 eV. → at tight fmax the binding constraint is the
    **step budget, not the optimizer** — reinforces the existing "keep fmax
    loose (0.1–0.15), do NOT tighten" stance. neb-ode is the right optimizer
    *for the screening regime*.
  - **Doc updated in autocatpath** (`docs/CONFIG.md` §NEB optimizer, pushed
    catpath `93c9bca`): corrected the "fire = most robust" claim and added the
    real-MACE data point + step-budget caveat.
  - **NEXT (precis-side, pending user go-ahead — prod ship+deploy):** wire
    autocatpath 0.8 — bump pin `>=0.7`→`>=0.8` (`pyproject.toml:252,262`) +
    `_AUTOCATPATH_CACHE_EPOCH` `0.7.0`→`0.8.0` (`compute.py:429`), and set
    `search.neb_optimizer: neb-ode` on the **neb tier** (overlay in
    `_apply_tier_config`, `compute.py:68`, beside `best_first`; explicit
    caller config still wins, verify tier untouched). **Keep the other
    optimizations** (`mlip.dtype: mixed`, `neb_schedule: best_first`, cueq).
    Then a **proper full-pathway benchmark** once integrated (single-edge here
    can't see multi-saddle path-dependence on the hard NO→N+O dissociation
    edge — validate neb-ode converges there before trusting it wholesale; best_first
    prunes many hard edges, bounding the downside). Deploy is TWO steps
    ([[catpath-dev-deploy]]).

- **autocatpath 0.9.0 shipped batched NEB (the GPU-utilisation lever) — but
  it was BROKEN on MACE; found + fixed + pushed (catpath `9d587e5`).**
  0.9.0 (`b146f58`) added `search.neb_batched` + `neb.BatchedNEB`: one MLIP
  forward per step over all interior images (physics-identical, guarded by a
  runtime self-check that degrades to serial on mismatch). Correct design, but
  the MACE fast path (`calculators._mace_batched_evaluator`) is **untested in
  CI** (`test_neb_batched.py` is EMT-only by the author's choice — the mace
  batch is "validated on target hardware, not here"). Benchmarked locally on
  **mace 0.3.16 (spark's exact version)**: the batched forward raised
  `RuntimeError: both inputs should have same dtype` and the guard **silently
  fell back to serial every time** → `neb_batched: true` would have bought
  **zero** GPU benefit in prod. Root cause: the collated graph is built at
  torch's default float32 while the NEB model is float64; the batched path
  never cast the batch to the model dtype (the serial ASE calc does).
  **Fix (2-line, pushed `9d587e5`):** cast the batch's float tensors to
  `next(model.parameters()).dtype` (ints untouched). Verified: batched vs
  serial now agree to **dE 1.1e-6 eV / dF 6.7e-6 eV/Å**, guard passes, full
  BatchedNEB runs without fallback, barrier unchanged (0.2732→0.2734 eV).
  - **Reaches prod only via a new PyPI release** — playbook 44 installs
    autocatpath from PyPI per the `>=` pin, NOT git main ([[catpath-dev-deploy]]).
    So the fix needs a **catpath `0.9.1` GH release → PyPI** before it's on
    spark. (CI gap remains: the mace batched path is still untested — a
    `pytest.importorskip("mace")` regression test would catch this class, but
    the author deliberately keeps mace out of CI; flagged, not added.)
  - **Precis integration (pending):** pin `>=0.9.1`, epoch bump, set
    `neb_optimizer: neb-ode` + `neb_batched: true` on the neb tier, keep
    mixed/best_first/cueq. Then measure ONE seed on spark: wall + GPU util via
    `tegrastats` (pynvml/nvidia-smi say "Not Supported" on GB10).

- **Operational state 2026-08-09:** spark idle, precis-worker **running**
  (restarted ~13:16 UTC, not by this session), GPU 0%, engine still **0.7.0**
  (0.8/0.9 never deployed). **130 `autocatpath_seed` job rows** (`kind='job'`,
  `deleted_at IS NULL`): **105 unclaimed/claimable-now**, 24 stale
  expired-lease (the known leak, [[catpath-barrier-trust]]), 1 live-lease. The
  running worker will start grinding a **0.7-engine** (dense-BFGS, all-edges,
  ~10 h) seed at the next claim — so these want killing before anything else.

- **cuequivariance accel — shipped + deployed 2026-08-08.** Wired into
  `roles/autocatpath` (install + import-verify, gated to the ML-backend host)
  and installed on spark via playbook 44. Fast path confirmed engaged (no
  "Cuequivariance acceleration will be disabled" banner). Necessary but
  **not sufficient**: seeds still run float64 and blow the wall by 3–4×.

- **autocatpath 0.7 precision + scheduling levers WIRED into precis** (this
  rev — `quest/compute.py` + `pyproject.toml`). Two independent per-seed cost
  cuts, both accuracy-safe:
  - **`mlip.dtype: mixed`** (autocatpath 0.6) — float32 coarse relax to
    `max(3·fmax, 0.15)` then float64 refine to `fmax`; **NEB tops stay
    float64**, so reported barriers keep float64 accuracy (qu164903 measures
    barrier tops) while the descent runs at ~float32 speed. Injected via
    `setdefault("dtype","mixed")` beside the existing `device=cuda` on the
    GPU-routed run-config.
  - **`search.neb_schedule: best_first`** (autocatpath 0.7) — the big NEB-cost
    lever on a bushy network: relax every endpoint first, then run NEBs
    frontier-first on the lowest-optimistic-span route and prune any route
    whose *optimistic* span (an unrefined edge = its thermodynamic climb, a
    strict **lower bound** on its true TS) can't come within `neb_margin`
    (0.2 eV default) of the best refined span. Pruning is provably safe — it
    can only skip work, never hide a competitive route — so it buys back the
    NEB cost of the far-uphill side-product forks (NH2OH branch, N2/N2O
    coupling, …). Overlaid on the **neb tier only** (`_apply_tier_config`); the
    **verify tier stays exhaustive** so the final answer on the winning
    candidate carries no honest-absence caveat. An explicit
    `search.neb_schedule` in the reaction config wins.
  - Pin bumped `autocatpath>=0.4` → `>=0.7` (`catalyst` + `catalyst-gpu`);
    **`_AUTOCATPATH_CACHE_EPOCH` `0.4.0` → `0.7.0`** (was mandatory then;
    since 2026-08-09 the token derives from the pyproject pin floor — the
    pin bump alone re-keys, no hand-bump — re-keys every
    candidate so the 234 unreaped stale-engine seeds re-dispatch clean on 0.7,
    [[catpath-barrier-trust]]).

- **Deployed 2026-08-09** (both steps — the engine bump needs two, see
  [[catpath-dev-deploy]]): `scripts/deploy` shipped the `compute.py` dispatch
  change (epoch/dtype/best_first) to melchior + all venvs, and
  `ansible-playbook playbooks/44-autocatpath.yml` pulled **autocatpath 0.7.0**
  into the spark GPU worker venv (verified `autocatpath.__version__ == 0.7.0`,
  worker restarted). **Open confirmation:** watch the first fresh seed
  dispatched on 0.7 — does it land `result.json` *under* the ~3 h wall, and
  does its `results.json.neb_schedule` list `skipped` edges (proof the prune is
  firing)? The old-engine seed `auj_2t19` was still running float64 at deploy
  time and will finish/wall-kill on its own; the epoch re-key re-dispatches the
  234 stale candidates clean on 0.7.

- **`pose_count` is dead — removed in 0.6.0.** Never consumed by the engine
  (pose diversity comes from `search.seeds` + `bind_reseat_attempts`); old
  configs carrying it still parse. Not a lever.

- **Root cause of the overrun is the NEB itself: dense-Hessian BFGS × a
  thousands-of-evals-per-edge budget in float64 — a PRE-EXISTING autocatpath
  engine cost, NOT a device/config/precis bug (diagnosed 2026-08-09, one wedged
  0.7 seed killed + engine code traced).** Ruled OUT, with proof:
  - **Device/GPU/MACE are healthy on the GB10.** Isolated on spark's
    `/opt/precis/venv`: `torch 2.13.0+cu130`, GB10 cc (12,1)=sm_121, `arch_list`
    has `sm_120` (covers it — raw matmul 0.48 s, no `no kernel image` error), and
    **`mace_mp(device='cuda', default_dtype='float64')` places the model on
    `cuda:0` and runs a 36-atom float64 forward in ~1.2 s.** So no arch mismatch,
    no silent CPU fallback, no `CUDA_VISIBLE_DEVICES` masking (it was simply
    unset → GPU 0 visible). The `pynvml: Not Supported` line is benign; the
    "pynvml → CPU fallback" reading was wrong.
  - **The 153 GB "memory blowup" was a mis-read.** spark is 128 GB RAM + 16 GB
    swap, swap stayed 0 B, no OOM — so a 153 GB *RSS* is physically impossible;
    that figure is almost certainly **VSZ** (CUDA reserves huge virtual address
    space on unified-memory hw). There was no real leak-to-OOM. DOF math kills
    the dense-Hessian-as-150 GB theory too: `ndofs = 3·(nimages−2)·natoms` =
    3·3·38 ≈ 570 for this 5-image/38-atom edge → Hessian ~2.6 MB, not GB.
  - **What the NEB actually costs.** `neb.py` uses ASE **`BFGS`** (dense Hessian:
    `H0 = eye(ndofs)`, an `eigh(H)` every step → BLAS-multithreaded, ≈6 cores
    pinned at 0 % GPU between force calls — explains the 666 % CPU signature) and
    drives an enormous eval budget per edge: tether-settle ramp (4 passes) ×
    `settle_steps` × 5 images + climb (`max_steps`) × 5 images + `neb_retries` +
    `neb_auto_retry` ≈ **~5,000 float64 MACE force+grad evals for one edge**. At
    ~1.2 s/eval that is **~100 min per edge**, serial — a full pathway has many
    edges. That is the wall overrun, mechanistically.
  - **Pre-existing, exposed not caused by 0.7.** `git diff v0.6.0 v0.7.0 --
    neb.py` is **empty**. `dtype: mixed` (a 0.6 feature) forces the NEB to
    float64 (`precise=True`) → 2× cost/eval; best_first (0.7) only changes
    *which* edges get NEB'd (so the far-uphill `[NO->N+O]` dissociation now
    runs). The cost was always there; the levers reweighted exposure to it.
  - **Open — was it hung or just slow?** ASE `BFGS(logfile=None)` emits no
    per-step line, so a silent log ≠ a hang; a ~100 min/edge cost means "1 h in,
    no result" can be normal-but-unacceptable, not wedged. Distinguishing a genuine
    stall (IDPP interpolation at NEB entry — CPU-only `MDMin`, the first thing
    that runs; or a torch float64 per-eval retain-graph leak) from plain crushing
    cost needs a **live `py-spy dump`** — which was NOT installed in the venv, and
    the process is now killed. Settle on the next live seed: `uv pip install
    py-spy` into `/opt/precis/venv`, let one seed reach NEB, dump within seconds.
  - **Fixes (all in the catpath engine repo, not precis):** (1) swap NEB
    optimizer `BFGS`→`FIRE`/`LBFGS` in `neb.py:100,106` — kills the O(ndofs²/³)
    dense-Hessian smell, standard for NEB, correct regardless of cause; (2) cut
    the per-edge eval budget (`neb_images`, `neb_retries`, `neb_auto_retry`,
    `bind_tether_ramp`, `neb_max_steps`); (3) incremental checkpointing so a
    wall-kill degrades instead of annihilating the edge. best_first (shipped)
    already prunes edges; these attack the per-edge cost it can't.
  - **Operational note:** `precis-worker` is **stopped on spark** (halts ALL
    spark passes, not just autocatpath) as of this investigation — restart it
    (or restart with pathway disabled) once a fix path is chosen; don't leave the
    node idle indefinitely.
- **Still open regardless of per-eval speed:** incremental checkpointing so a
  wall-kill degrades instead of annihilating a seed's work, then sharding
  per-intermediate/edge. Also operational — **now CONFIRMED, not just
  suspected:** ~135 `autocatpath_seed` rows are stuck `claimed` with **expired
  leases** (oldest ~6 days), never reaped after wall-kill — a real leak (the
  wall-timeout path leaves no terminal/reaped row, so the harvest/promotion loop
  can't see them). They don't hold the GPU slot now, but the accumulation is
  systemic. Fix = the wall-timeout path must record a reaped failure + a sweeper
  to reclaim expired-lease claims.

Owner `src/precis_pathway/runner.py` + `src/precis/quest/compute.py`. Needs
design (checkpointing/sharding); the speedup levers are wired, pending deploy.

Related: autocatpath-seed-failure-diagnosis.md (slow-overrun vs fast rc=-15
crash population), autocatpath-lease-churn.md (re-lease churn duration).
