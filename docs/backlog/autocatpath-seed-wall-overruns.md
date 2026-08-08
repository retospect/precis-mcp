# autocatpath_seed genuine overruns — reduce cost, checkpoint

26 of 108 failures cluster at the runner's ~3 h deadline; the 5400 s compute
budget fired once, so it is not the binding constraint. Levers in preference
order: `pose_count` 6 → 3–4 (dominant multiplier), incremental checkpointing
so a kill degrades instead of annihilating, then sharding
per-intermediate/edge. Decided constraint: do NOT tighten `fmax` — at
0.1 eV/Å (`neb_fmax` 0.15) it is already 2× looser than the ASE/OC20 norm.
New lever (2026-08-08 live spark probe): MACE runs its slow path on spark —
`cuequivariance` absent + float64, so every equivariant tensor-product is
unaccelerated on the GB10 (GPU-bound, not hung). Install
`cuequivariance-torch` (+ cu12 ops) into spark's autocatpath venv **via the
deploy role** (a deploy-extras-gap) and run relaxation in float32 —
complementary to the `pose_count` cut; needs an aarch64/Blackwell
wheel-compat check first, not assumed drop-in.
Owner `src/precis_pathway/runner.py` + `src/precis/quest/compute.py`. Needs
design.
