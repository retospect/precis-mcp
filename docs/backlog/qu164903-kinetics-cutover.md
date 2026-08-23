---
status: draft
title: qu164903 kinetics cutover — prod rollout checklist
prio: high
---

# qu164903 kinetics cutover — prod rollout checklist

<!-- Ops runbook, not a code-change item: the code (catalyst_seed
RUBRIC_OBJECTIVES, frontier.py per-quest axes/viewport/$/rate, the web
axis picker, the PNG twin, the tick prompt's tradeoff guidance) ships
independently and is dark until this checklist runs. Every prod-mutating
step below is executed by Reto, never an agent (`prod-mutation-needs-
user-permission` in memory) — an agent may PREPARE the exact command but
must hand it to the user rather than run it. -->

## Motivation / why

The frontier's default rubric moved from `{barrier, energy, selectivity_margin,
poison_margin}` to `{log_tof, atom_cost, selectivity_margin, poison_margin}`
(`catalyst_seed.RUBRIC_OBJECTIVES`) — `barrier` demotes to a context scalar
(TOF is computed FROM it; ranking on both is a redundant axis) and the new
kinetics/economics measures (`tof`/`log_tof`/`log_tof_p5`/`log_tof_p95`/
`kinetics_trusted`/`kinetics_note`/`drc_top`, later `atom_cost`/
`atom_cost_dearest`) only exist once autocatpath ships the in-process
microkinetics solve (`precis_pathway.runner.run_kinetics` +
`_dispatch_common._kinetics_scalars`; the kinetics module exists from catpath
0.15.0, but the cutover targets >= 0.17 — the current tree with the guard
bracket + rule-based verdict). The code side of this
(precis) ships independently of catpath's own release and of the prod quest's
own `meta.rubric_objectives` — nothing here mutates automatically. This item
is the ordered checklist to actually cut the running catalyst quest (qu164903)
over once catpath 0.17 exists.

## In scope — ordered checklist

> **Interim channel (2026-08-23): private wheel, no PyPI.** catpath stays
> private for now, and its auto-publish is OFF anyway (a GitHub release does
> NOT reach PyPI; publishing is a manual `workflow_dispatch` of
> `workflow.yml` with a typed version confirm). Until publication, 0.17.0
> reaches the sparks as a locally-built wheel (`uv build` in `~/catpath`)
> via the `roles/autocatpath` wheelhouse (`/opt/precis/wheels` on each
> host — installed over the constraints pin every run, survives redeploys;
> see `deploy/roles/autocatpath/defaults/main.yml`). Steps 1–2 below are
> DEFERRED to publication time; step 3's playbook-44 half runs now with
> `-e autocatpath_wheel=$HOME/catpath/dist/autocatpath-0.17.0-py3-none-any.whl`.

1. **(Deferred until publication) Publish catpath >= 0.17.0 to PyPI** from
   green CI: manual `gh workflow run workflow.yml -R retospect/catpath -f
   confirm=0.17.0` (OIDC trusted publishing; uploads both `autocatpath` and
   the `catpath` alias). CI lint was fixed green at catpath `0092f96`.
2. **(Deferred until publication) Bump the precis pin** in `pyproject.toml`:
   `autocatpath>=0.13.0` → `>=0.17.0` in BOTH the `catalyst` extra (~line
   311) and the `catalyst-gpu` extra's `autocatpath[mace]>=0.13.0` (~line
   321), re-lock, ship. CANNOT ship while the package is private — `uv lock`
   has to resolve the pin from PyPI. After this ships + deploys, DELETE the
   wheelhouse wheels (`/opt/precis/wheels/autocatpath-*.whl` on spark/
   castor/pollux) — a lingering wheel overrides any newer published pin.
3. **Deploy the code**: `scripts/deploy` (cluster venvs + melchior — the
   dispatch/harvest side of s3 lives in precis, so melchior needs it) AND
   `ansible-playbook playbooks/44-autocatpath.yml` from the synced main
   tree's `deploy/` (sparks' worker venvs: reinstalls precis-mcp@main +
   engine; NOT covered by `redeploy-precis.yml` — `catpath-dev-deploy` in
   memory). First wheel run adds
   `-e autocatpath_wheel=$HOME/catpath/dist/autocatpath-0.17.0-py3-none-any.whl`;
   later runs pick the wheelhouse copy up automatically.
4. **Prod write — update qu164903's rubric** (Reto only, per
   `prod-mutation-needs-user-permission`; an agent prepares the exact
   one-off CLI/SQL and hands it over, does not run it):
   `meta.rubric_objectives` → the new four-axis vector
   (`catalyst_seed.RUBRIC_OBJECTIVES` — copy verbatim, do not hand-retype).
   Optionally also update `meta.reaction_config` with kinetics conditions
   (temperature/pressures) if the worked example needs them for
   `run_kinetics` to have something to solve over — check
   `precis_pathway.runner.run_kinetics`'s config contract before deciding
   this is needed; if the conditions are already implied by the existing
   `reaction_config`, skip it.
5. **Re-key + redispatch**: `precis quest reset-compute 164903` then
   `precis quest redispatch 164903` (the CLI's `id` arg is the numeric
   ref id, not the `qu`-prefixed handle; prod one-off CLI, `prod-one-off-
   cli-write` recipe in memory — DSN from the melchior web plist,
   `--database-url`, run remotely). Re-keying is already guaranteed by the
   `_AUTOCATPATH_SUMMARY_REV` bump to `s3` shipped alongside this slice
   (`precis/quest/compute.py`) — a pre-s3 aggregate carries none of the
   kinetics keys, so harvest re-derives every candidate's `tof`/`log_tof`
   from scratch on redispatch; no separate migration needed for that part.
6. **Resume dispatch**: clear qu164903's job type out of
   `precis_suspended_job_types` in `deploy/group_vars/all.yml` (confirm the
   current value first — the loop may already be un-suspended per
   `qu164903-loop-fixes-followthrough` in memory; don't double-clear an
   already-empty value), ship, redeploy.

## Explicitly NOT in scope

- Any catpath engine code — that repo cuts its own release on its own
  schedule; this item only reacts to a release existing.
- Backfilling `atom_cost` historically on already-measured candidates —
  it's a LOCAL, sim-free computation (mass-weighted $/kg off the
  composition already on each candidate's `structure.meta`), so once the
  code + rubric are live it backfills itself on the next harvest pass; no
  separate backfill script is needed.
- Any other quest's rubric — this checklist is qu164903-specific; a new
  catalyst quest minted after this ships gets the new four-axis default
  automatically via `catalyst_seed.RUBRIC_OBJECTIVES`.

## Acceptance criteria

- `qu164903`'s `meta.rubric_objectives` reads `log_tof`/`atom_cost`/
  `selectivity_margin`/`poison_margin` in prod.
- A tick against qu164903 harvests `tof`/`log_tof` (or
  `kinetics_trusted=False` + `kinetics_note`) onto at least one candidate,
  and the frontier hub (`/refs/quest/164903`) plots the new axes by
  default (no `?fx=&fy=` override needed).
- The quest's dispatch is un-suspended and ticking again.
- Fan-out proven (the last unproven piece of the 3-spark cutover): fresh
  `autocatpath_seed` jobs spread across spark/castor/pollux — check
  `target_node` distribution on new rows (`kind='job'`,
  `meta->>'job_type'='autocatpath_seed'`, `deleted_at IS NULL`).

## Target + blast radius

`pyproject.toml` extras (2 lines), qu164903's own `meta` (prod write, one
quest), `deploy/group_vars/all.yml` (`precis_suspended_job_types`),
`44-autocatpath.yml`'s target venv. No other quest, no schema/migration.

## Open questions / decisions log

- ~~Whether `reaction_config` needs explicit kinetics conditions~~ —
  RESOLVED (2026-08-23, against catpath 0.17.0's `config.py`): no.
  `ConditionsConfig` defaults to standard conditions (298.15 K; every
  unlisted gas sits at the 1 bar reference) and `KineticsConfig` defaults
  are sane (sticking 1.0, product = the run's `target`), so
  `kinetics.solve` runs on the existing `reaction_config` unchanged. It
  emits a stated warning that the product pressure defaults to the 1 bar
  reference — acceptable: the frontier ranks candidates *comparatively*
  at identical conditions. Skip the optional half of step 4.
