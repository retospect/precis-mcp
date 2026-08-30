# Prod DB down after a caspar reboot — stale `postmaster.pid`

**Symptom.** asa/precis goes fully silent — no morning news, no responses, no
briefing. Every `claude_inproc` job stalls (they run on melchior's agent
worker) and asa-bot is mute. The cause is not the worker: it is the **prod DB
being down**.

caspar reboots nightly at 03:00 UTC (`daily-update-reboot.sh`, hardening
role), so this class of failure surfaces as a morning outage.

## Diagnostic chain

1. pgbouncer on caspar `100.126.127.107:6432` answers but returns
   `client_login_timeout (server down)` → the backend Postgres on `5432` is
   down, not the pooler.
2. Check `uptime` — a reboot in the last few hours is the tell.
3. Postgres is a **system LaunchDaemon**,
   `/Library/LaunchDaemons/com.postgresql.plist`, running `postgresql@17`;
   log at `/opt/homebrew/var/log/postgresql@17.log`.
4. A crash-loop every ~10s with
   `FATAL: lock file "postmaster.pid" already exists ... Is another
   postmaster (PID N) running?` — while **no postgres process exists** — is
   the signature.

**Why it wedges.** The stale `postmaster.pid` (data dir
`/opt/homebrew/var/postgresql@17`, file owned `deploy:admin` 0600) records a
PID that the reboot **reused for an unrelated process** (observed: the bge-m3
embedder `serve-embeddings`). Postgres conservatively refuses to start when
the lock's PID is alive, so KeepAlive restarts it into the same refusal
forever.

## Immediate recovery

`rm` the stale `postmaster.pid`. KeepAlive restarts Postgres within ~10s — no
sudo needed, the file is `deploy`-owned. The agent worker (melchior,
`/opt/mcps/venv`, find it with `pgrep -lf "profile agent"`) self-recovers on
its next poll; nothing else needs bouncing.

## Permanent fix (shipped 2026-07-05, two layers)

1. **Drain before reboot** (primary) — the nightly `daily-update-reboot.sh`
   fast-drains Postgres before `shutdown -r`, so no stale pid is created.
   Deployed to all Mac hosts.
2. **Guarded launcher** (backstop, for power-loss/panic where a drain can't
   run) — the plist's bare `postgres -D …` routes through a launcher that
   removes a stale `postmaster.pid` **iff no postmaster is running**
   (`pgrep -f <pgbin>`), then `exec`s postgres. Safe under KeepAlive; proven
   by simulating the exact PID-reuse failure.

Both live in the `postgres` Ansible role
(`deploy/roles/postgres/templates/pg-guarded-start.sh.j2` +
`postgresql.plist.j2` ProgramArguments), so a converge can't revert them —
the original was a hand-edit the role would have overwritten.

**Known drift.** The role's `restart postgresql` handler is `kickstart -k`,
which uses the cached job definition and does not hot-swap the plist: a
converge *stages* the new plist and the nightly reboot *activates* it. caspar's
live definition may therefore still point at the hand-edited
`/opt/homebrew/etc/pg17-guarded-start.sh`, orphaned once the role deploys the
`pg-guarded-start.sh` name. Original plist backed up at
`/Library/LaunchDaemons/com.postgresql.plist.bak-20260705`.

## Ops notes

- `sudo -n` works passwordless for `deploy` on caspar.
- Reloading a system LaunchDaemon needs `launchctl bootout system/com.postgresql`
  then `bootstrap system <plist>` — `kickstart -k` won't re-read the plist.
- A bootstrap immediately after a bootout often fails `EIO 5` (label not yet
  released) — **retry after a short sleep**.
- Only caspar hosts Postgres; this does not apply to the DB-less nodes.

Related: [`prod-one-off-cli`](./prod-one-off-cli.md),
[`restart-worker-and-watch`](./restart-worker-and-watch.md).
