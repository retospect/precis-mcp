---
status: idea
title: the canary deploy's heartbeat verify calls scripts/prod-psql from a cwd where it does not exist
---

# `PRECIS_DEPLOY_CANARY`'s verify step cannot find `scripts/prod-psql`

## What
`scripts/deploy` does `cd "$CLUSTER_DIR"` (`scripts/deploy:527`) before the
ansible phases. The canary heartbeat verify then invokes the helper by a
bare relative path (`scripts/deploy:775`):

```bash
_ts_epoch="$(PRECIS_PROD_PSQL_OPTS="-At" scripts/prod-psql \
    "SELECT extract(epoch from ts)::bigint FROM host_heartbeat WHERE host = '${DB_HOST}';")" \
    || die "canary verify: scripts/prod-psql failed reaching prod — treated as red (fail closed)."
```

With the cwd at `$CLUSTER_DIR` that resolves to
`<checkout>/deploy/scripts/prod-psql`, which does not exist —
`scripts/prod-psql` lives at the repo root. Confirmed by inspection on
2026-09-26; `ls deploy/scripts/prod-psql` is a miss.

## Consequence
The `||` branch is a `die` with a **fail-closed** message that reads as
"could not reach prod", so the failure is indistinguishable from a real
prod-connectivity problem. Its text then claims a mixed-version fleet
(canary on new code, everyone else old) and prints a rollback hint — all on
what is really a path bug, before any heartbeat was ever queried.

## Scope
Only the `PRECIS_DEPLOY_CANARY` path. The normal deploy path never reaches
line 775, which is presumably why this has survived: if canary deploys were
routinely exercised, the first one would have failed.

Worth confirming whether canary is used at all before fixing — if it is
dead, deleting it beats repairing it.

## Fix shape
Use an absolute path: `"${REPO_ROOT}/scripts/prod-psql"`. `REPO_ROOT` is
already resolved at `scripts/deploy:145` and is unaffected by the later
`cd`.

Note the render-worktree change (2026-09-26) moves `$CLUSTER_DIR` into a
detached worktree for literal-sha targets. That neither causes nor worsens
this — `deploy/scripts/prod-psql` is absent in both trees — but it does mean
the fix should anchor to `REPO_ROOT`, not to the cluster dir.

## Provenance
Found by the `reviewer` agent while reviewing the render-worktree change,
as an out-of-scope observation; verified independently before filing.
