# autocatpath_seed genuine overruns — reduce cost, checkpoint

26 of 108 failures cluster at the runner's ~3 h deadline; the 5400 s compute
budget fired once, so it is not the binding constraint. Decided constraint: do
NOT tighten `fmax` — at 0.1 eV/Å (`neb_fmax` 0.15) it is already 2× looser
than the ASE/OC20 norm.

Status 2026-08-08:

- **cuequivariance accel — shipped + deployed 2026-08-08.** Wired into `roles/autocatpath`
  (install + import-verify, gated to the ML-backend host) and installed on
  spark via playbook 44. Fast path confirmed engaged on a real seed — no
  "Cuequivariance acceleration will be disabled" banner in `stdout.log`.
  Necessary but **not sufficient**: a post-install seed (`6abglq07`) still ran
  >106 min because MACE is still in float64 (`Using float64 for
  MACECalculator, which is slower but more accurate`).
- **Precision levers landed in `autocatpath` 0.6.0** (commit `44aa165`,
  2026-08-08) — the remaining per-eval speedup, and the critical path for the
  overrun:
  - `mlip.cueq` (auto|on|off), **default `auto`** — cuEquivariance kernels used
    whenever `cuequivariance-torch` is installed. Already installed on spark,
    so cueq is *already* engaged (even on the deployed 0.4.x — MACE picks the
    lib up itself); no precis config needed.
  - `mlip.dtype` **still defaults to `float64`** — so the float32 win is OFF
    until precis sets it. `mixed` = float32 coarse descent to `max(3·fmax,
    0.15)` then float64 refine to `fmax`; **NEB always float64** (barrier tops,
    which is exactly what qu164903 measures), reported energies from the
    float64 stage. So `mixed` is the accuracy-safe speedup; plain `float32` is
    faster but noisier near `fmax`.
  - **Next-rev precis-side wiring** (deferred by decision, not in the cueq
    pass): (1) bump the pin `autocatpath>=0.4` → `>=0.6` in `pyproject.toml`
    (`catalyst` + `catalyst-gpu`); (2) **bump `_AUTOCATPATH_CACHE_EPOCH`
    `0.4.0` → `0.6.0`** in `quest/compute.py` — mandatory, else new-engine
    re-runs dedup-pin onto stale jobs ([[catpath-barrier-trust]]); (3) default
    `mlip.dtype: mixed` on the precis side — a `setdefault("dtype","mixed")`
    beside the existing `setdefault("device","cuda")` at `compute.py`'s
    run-config assembly; (4) apply the autocatpath role
    (`ansible-playbook playbooks/44-autocatpath.yml`, `--refresh-package
    autocatpath` pulls 0.6.0) — note `scripts/deploy`/`redeploy-precis.yml`
    does NOT include the autocatpath play, so the standalone playbook is
    required.
- **`pose_count` is dead — removed in 0.6.0.** It was never consumed by the
  engine (pose diversity comes from `search.seeds` + `bind_reseat_attempts`);
  old configs carrying it still parse. Do not chase it as a lever.
- Still open regardless of per-eval speed: incremental checkpointing so a
  wall-kill degrades instead of annihilating a seed's work, then sharding
  per-intermediate/edge.

Owner `src/precis_pathway/runner.py` + `src/precis/quest/compute.py`. Needs
design.
