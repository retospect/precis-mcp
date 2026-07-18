# Cluster log locations (where to look when triaging the fleet)

> Practical map of every log a health sweep needs. Hosts + roles are in
> the `cluster` repo `inventory/hosts.yml`; log paths come from the
> `roles/precis_*` LaunchDaemon/systemd templates. Keep this in sync when
> a role's `StandardOutPath` / `StandardOutput` changes.

## Hosts (SSH)

Reach every node over Tailscale. Bare `ssh <host>` works — `~/.ssh/config`
already pins `IdentityAgent none` for the cluster hosts (on-disk
`~/.ssh/cluster`), so an explicit `-o IdentityAgent=none` is redundant:

    ssh <host> '<cmd>'

| host       | OS / init      | role       | runs |
|------------|----------------|------------|------|
| melchior   | macOS / launchd| gateway    | system worker + **agent worker** (plan_tick/reviewers) + web + dream + cron-tick + litellm + asa-bot + anki-sync |
| caspar     | macOS / launchd| data (NFS) | system worker + embedder + Postgres (prod DB) + backups |
| balthazar  | macOS / launchd| scheduler  | system worker + embedder |
| spark      | Linux / systemd| inference  | system worker + embedder + GPU (relax/AlphaFold) |

## Log files (all under `/var/log/` unless noted)

| log | hosts | what |
|-----|-------|------|
| `precis-worker.log`        | all         | system-profile worker (embed, summarize, chunk_keywords, dispatch, nursery, reconcile, …). The big one (100s of MB–1 GB). |
| `precis-worker-agent.log`  | melchior    | agent-profile worker (structural/deep_review, `job_claude_inproc` plan_tick, quota_check). Owner `hermes`. |
| `precis-web.log`           | melchior    | FastAPI/uvicorn web UI. uvicorn lines have no leading timestamp. |
| `precis-embedder.log`      | all         | `serve-embeddings` (bge-m3), loopback `127.0.0.1:8181`. Health: `curl 127.0.0.1:8181/readyz` → `ready`. |
| `precis-watch.log`         | all         | ingest inbox watcher. |
| `precis-dream.log`         | melchior    | dream_agent (15-min cadence). |
| `precis-cron-tick.log`     | melchior    | cron-tick daemon. |
| `precis-anki-sync.log`     | melchior    | headless AnkiWeb sync. |
| `precis-reconcile.log`     | caspar      | duplicate/hygiene reconcile CLI runs. |
| `precis-heartbeat.log`     | all         | per-node liveness heartbeat. |
| litellm: `/opt/homebrew/var/log/litellm.log` + `litellm-error.log` + `litellm-watchdog.log` | melchior | LLM proxy (port 4000). 401s here = Anthropic auth. |
| asa-bot: `/Users/hermes/.asa/asa-bot.log` (needs `sudo`) | melchior | Discord bridge. Also relays nursery Discord alerts. |
| Shared crons: `/opt/shared/logs/` (macOS) / `/shared/logs/` (linux) | caspar hosts them | `backup-pg.log`, `backup-b2.log`, `backup-usb.log`, `backup-tests.log`, `daily_briefing/*.log`, `api-credits.log`, `pip-audit/audit.log`, `nginx-*.log`. |

Line format on the python logs: `YYYY-MM-DD HH:MM:SS,mmm LEVEL logger msg`.
Filter last 24h with e.g.:

    awk '/^2026-07-1[56]/{d=1} /^2026-07-1[0-4] /{d=0} d' /var/log/precis-worker.log \
      | grep -aE ' (ERROR|CRITICAL) |Traceback'

## The higher-signal check: nursery alerts in the DB

Log-grepping is noisy. The nursery already machine-detects health
conditions (dead-worker, dispatch-stall, worker-restart, spin-loop,
orphan, stuck-doable, budget) as `kind='alert'` rows. Query the currently
**open** ones (auth: `agent_rw` has SELECT; alert tags live in the `OPEN`
namespace as colon-strings — `alert-state:open`, `alert-source:…`,
`severity:critical`):

    ssh caspar 'psql -h 100.126.127.107 -p 6432 -U agent_rw -d precis_prod -F "\t" -tA -c "
    WITH a AS (
      SELECT r.ref_id, r.title, r.updated_at,
        max(CASE WHEN t.value LIKE '\''alert-state:%'\'' THEN split_part(t.value,'\'':'\'',2) END) state,
        max(CASE WHEN t.value LIKE '\''alert-source:%'\'' THEN substr(t.value,14) END) source,
        max(CASE WHEN t.value LIKE '\''severity:%'\'' THEN split_part(t.value,'\'':'\'',2) END) severity
      FROM refs r JOIN ref_tags rt ON rt.ref_id=r.ref_id
                  JOIN tags t ON t.tag_id=rt.tag_id AND t.namespace='\''OPEN'\''
      WHERE r.kind='\''alert'\'' AND r.deleted_at IS NULL
      GROUP BY r.ref_id, r.title, r.updated_at)
    SELECT severity, source, to_char(updated_at,'\''MM-DD HH24:MI'\''), left(title,85)
    FROM a WHERE state='\''open'\'' ORDER BY (severity='\''critical'\'') DESC, source, updated_at DESC;"'

Only `dead-worker`, `dispatch-stall`, `worker-restart`, `budget`, and
`quota_check:auth` are `critical` (they page via asa-bot Discord); the rest
(`orphan`, `stuck-doable`, `spin-loop`, `stalled-recurring`) are info/warn
housekeeping.
