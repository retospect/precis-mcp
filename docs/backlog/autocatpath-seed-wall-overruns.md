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
    **`_AUTOCATPATH_CACHE_EPOCH` `0.4.0` → `0.7.0`** (mandatory — re-keys every
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

- **Still open regardless of per-eval speed:** incremental checkpointing so a
  wall-kill degrades instead of annihilating a seed's work, then sharding
  per-intermediate/edge. Also operational: 234 seeds have accumulated unreaped
  over 5 days with no terminal event — verify the wall-timeout path records a
  reaped failure (a wall-killed seed that leaves no reaped row is invisible to
  the harvest/promotion loop).

Owner `src/precis_pathway/runner.py` + `src/precis/quest/compute.py`. Needs
design (checkpointing/sharding); the speedup levers are wired, pending deploy.
