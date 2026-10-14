---
status: in-progress
title: release deploy tolerates common trouble and does less work (~10 min, no human)
pillar: platform
---

# Cycle-time deploy fixes

Approved direction (Reto 2026-10-14): a worker host that is unreachable is
skipped automatically with a loud warning and caught up on the next deploy;
only the roles a change touches run; when the CI verdict is too old the
script re-runs CI itself instead of refusing.

## Motivation / why

r33 (2026-10-14): balthazar dropped off Tailscale mid-deploy (03:00Z), then
castor (07:05Z); each aborted the whole deploy and needed a manual resume,
then the 6 h verdict-age guard refused the resume. r34: ~25–30 min, mostly
the pythonorg TLS assert retries, embedder `/healthz` waits, and every role
on every host.

## In scope

1. `scripts/deploy` pings per host; a host that fails and is not critical
   (critical = the `data` + `gateway` inventory groups — DB/pgbouncer and
   web/MCP) is skipped with a loud WARNING and recorded for the round
   journal (`skipped_hosts`, with the sha it still runs). Same for a
   non-critical host that drops mid-playbook. `PRECIS_DEPLOY_SKIP_HOSTS`
   still works. `scripts/round status` shows lagging hosts;
   `scripts/round deploy --catch-up` re-runs the pinned rollout so
   returning hosts install.
2. `scripts/release` re-runs CI on the same sha (`gh run rerun`) when the
   verdict is older than the limit, waits for green, then proceeds; still
   refuses on red. The 6 h default is unchanged.
3. `scripts/deploy` diffs installed sha → target and passes `--skip-tags`
   for the optional imports in `redeploy-precis.yml` the change does not
   touch (pythonorg, the code-task image, and — for web-only or
   non-runtime diffs — the sha-baking image/asa/sandbox plays). Unknown or
   unmapped paths fail safe to a full run; so do a missing deploy marker,
   an overlay change, and a fleet with lagging hosts. Disable with
   `PRECIS_DEPLOY_ROLE_SCOPE=0`.
4. `roles/pythonorg`: the live TLS handshake assert runs only when the
   series version moved or the CA store was just populated (an install),
   not on every converge. The codesign/Developer-ID assert still runs
   every time.

## Explicitly NOT in scope

- No change to `DEFAULT_MAX_CANDIDATE_HOURS` (6 h stands, Reto 2026-10-12).
- No new privilege, no prod writes beyond the deploy itself.
- Not (ii) of `deploy-renders-only-precis-roles.md`: non-precis roles
  (backups, monitoring, pgbouncer) are still not rendered by this playbook.
  The option-(i) advisory print landed alongside (ADVISORY lines after each
  deploy naming the playbook to run by hand); the (ii)/(iii) pick stays
  with Reto.

## Acceptance criteria

- A deploy with a down non-critical host exits 0 with a WARNING, records
  the host + its pre-deploy sha, and `--limit`s it out of the rollout; a
  down `data`/`gateway` host still aborts. A mid-run drop on a
  non-critical host is marked skipped and the deploy completes; on a
  critical host it still fails.
- `round status` shows skipped hosts; `round deploy --catch-up` installs
  them on a second run and clears the record.
- `scripts/release` on a stale verdict triggers `gh run rerun`, waits for
  green, deploys; a red re-run still exits 2.
- A `src/precis`-only diff skips `pythonorg` and `34-code-task-image`; a
  `src/precis_web`-only diff also skips the tts/agent-image/asa/sandbox
  plays; an unmapped path runs everything.
- The pythonorg TLS assert is `when`-gated on `not pythonorg_up_to_date`
  (or a just-populated CA store); the codesign assert is unchanged.

## Target + blast radius

`scripts/deploy`, `scripts/round`, `scripts/release`,
`scripts/lib/deploy-state.sh`, `deploy/redeploy-precis.yml` (import tags
only), `deploy/roles/pythonorg/tasks/install_one.yml`. All controller-side;
the fleet sees only fewer tasks or fewer failures, never new actions.

## Open questions / decisions log

- Critical set = `data` + `gateway` groups exactly (per the brief).
- The skipped host's "sha it still runs" is the pre-deploy marker sha —
  the only honest value for a host that can't be asked.
- Role scoping maps by changed paths (above); anything unmapped deploys
  everything, same posture as 0b2's `full` default.
