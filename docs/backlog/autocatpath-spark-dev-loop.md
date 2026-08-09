# autocatpath: on-spark dev loop (measure + iterate on the GPU box)

**Why.** Deployed catpath 0.7.0 does **not** use the GPU. Measured
2026-08-09 while a real seed (`no_to_nh3_pd`, PID 574501, `device=cuda`,
`dtype=mixed`, medium, 5 images, best_first) ran on spark: `nvidia-smi dmon`
= **SM 15 %, 19 W** (near-idle) against **622 % CPU**. The path is CPU-bound
in the serial ASE NEB loop; the GPU is cold. neb-ode (fewer evals, 0.8.0) +
batched NEB (one MACE forward across all interior images → high SM%, 0.9.0,
dtype-fix `9d587e5` on catpath main) target exactly this. spark has working
`nvidia-smi`/`nvidia-smi dmon` (NOT `tegrastats` — not installed on this DGX
Spark; supersedes the earlier tegrastats note).

**Goal.** Iterate on catpath GPU code directly on spark (no
release→PyPI→ansible round-trip) and measure wall + SM% per knob combo.

## Ground truth (spark, 2026-08-09)
- host `spark`, aarch64, user `deploy`, HOME `/home/deploy`, `/` 2.4 T free.
- prod engine venv `/opt/precis/venv`: torch `2.13.0+cu130` (cuda True),
  mace `0.3.16`, autocatpath `0.7.0`; hosts both `precis_pathway` and
  `autocatpath`. **Never mutate this venv** (prod workers run from it).
- tooling: `git` 2.43, `python3` 3.12; **`uv` absent**, node/claude TBD.
- prod workers on spark run as `deploy` (not launchd): `precis worker
  --profile all` (claims seeds) + `--only job_ssh_node`.

## Proposed setup ("catpath dev loop on spark")
1. **Checkout, zero prod mutation:** clone github.com/retospect/catpath to
   `/home/deploy/dev/catpath` at `main` (carries batching dtype-fix `9d587e5`).
2. **Run mechanism = PYTHONPATH overlay** (no new venv, no wheel-sourcing):
   `PYTHONPATH=/home/deploy/dev/catpath/src /opt/precis/venv/bin/python …`
   → prod's torch/mace/precis_pathway with the **dev** `autocatpath`
   shadowing the 0.7.0 site-packages copy. Edit-run loop = `git pull` + rerun.
   Fallback (only if we need different deps): full isolated venv from
   playbook-44's torch cu130 index.
3. **Bench harness** in the checkout: takes the captured seed `request.json`
   (`no_to_nh3_pd`) as fixture, runs `precis_pathway.runner` (or catpath
   `neb_barrier` directly) over grid {optimizer: bfgs|neb-ode} ×
   {neb_batched: off|on} on cuda, with `nvidia-smi dmon` sampling in the
   background; prints wall, mean/peak SM%, barrier drift.
4. **Claude Code on spark (host-native dev):** install CLI (needs node —
   check/install), copy `~/.claude/{settings.json,agents,commands}` from the
   Mac; **auth = interactive `claude login` on spark preferred over copying
   `.credentials.json`** (copying a token to a shared cluster node spreads a
   secret — user's call). Small catpath-focused CLAUDE.md in the checkout.
5. **Clean-room for benchmarks (prod-mutating → user runs):** kill the live
   0.7 seed (PID 574501) and hold seed dispatch so the dev loop owns the GPU;
   restore after. Stop the dev loop before re-enabling the prod worker so they
   don't contend for the GPU.

## Invariants
- Never `pip install -e` into `/opt/precis/venv`; all dev under
  `/home/deploy/dev`.
- Dev loop and prod worker must not run heavy GPU work simultaneously.
- catpath ships by `git push origin main`; reaches prod only via GH release →
  PyPI + playbook 44 (+ mandatory `_AUTOCATPATH_CACHE_EPOCH` bump). The
  on-spark loop is for measurement/iteration, not a prod path.

## Open decisions (for user)
- Auth: interactive `claude login` on spark vs copy credentials?
- User/dir: `deploy` + `/home/deploy/dev` ok, or dedicated dev user?
- Quiesce now: kill seed 574501 + hold jobs for a clean first measurement?
