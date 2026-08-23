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

1. **Cut a catpath release >= 0.17.0** from green CI: `gh release create` in
   the `retospect/catpath` repo, OIDC-published to PyPI (catpath's install
   channel is release-gated, not a raw git dependency — see
   `docs/backlog/autocatpath-0141-pin-bump.md`'s note on this for the prior
   pin bump). Confirm the release actually carries `run_kinetics` /
   `_kinetics_scalars` (the s3 harvest keys `_AUTOCATPATH_SUMMARY_REV` in
   `precis/quest/compute.py` already expects) before proceeding.
2. **Bump the precis pin** in `pyproject.toml`: `autocatpath>=0.13.0` →
   `>=0.17.0` in BOTH the `catalyst` extra (~line 311) and the
   `catalyst-gpu` extra's `autocatpath[mace]>=0.13.0` (~line 321). Ship
   normally (`/land` or `/go`) — the gate container resolves the new pin,
   no PyPI dependency for catpath itself since the extras name it directly.
3. **Deploy the code**: `scripts/deploy` (cluster venvs — the engine-side
   `catalyst`/`catalyst-gpu` extras) AND
   `ansible-playbook deploy/playbooks/44-autocatpath.yml` (spark's GPU
   engine venv — NOT covered by `redeploy-precis.yml`, same two-step shape
   as every prior autocatpath version bump, see `catpath-dev-deploy` in
   memory).
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

## Target + blast radius

`pyproject.toml` extras (2 lines), qu164903's own `meta` (prod write, one
quest), `deploy/group_vars/all.yml` (`precis_suspended_job_types`),
`44-autocatpath.yml`'s target venv. No other quest, no schema/migration.

## Open questions / decisions log

- Whether `reaction_config` needs explicit kinetics conditions (T,
  pressures) added for qu164903, or whether `run_kinetics` derives
  reasonable defaults from the existing config — resolve against
  `precis_pathway.runner.run_kinetics`'s actual signature once catpath
  0.17 lands, before step 4.
