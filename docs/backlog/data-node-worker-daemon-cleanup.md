# Deploy removes the data node's worker venv but leaves its launchd daemon

**Found** 2026-08-14, investigating caspar's stale `host_heartbeat`.

The `redeploy-precis.yml` play "Remove vestigial precis venvs off non-owning
hosts" absents `/opt/precis/venv` on `data` (worker decommissioned off the DB
node 2026-08-11), but nothing removes or boots out the
`com.precis.worker` LaunchDaemon there. Result on caspar after the
2026-08-14 deploy: launchd KeepAlive respawns `exec
/opt/precis/venv/bin/precis` → exit 126 in a tight loop, spamming
`/var/log/precis-worker.log` indefinitely, and the stale `host_heartbeat`
row reads as an outage to anyone diagnosing the fleet.

Fix (playbook, `data`-scoped like the venv removal):

- `launchctl bootout system/com.precis.worker` + `disable` + absent the
  plist `/Library/LaunchDaemons/com.precis.worker.plist` on `data` hosts.
- Consider deleting the node's `host_heartbeat` row (or a tombstone) so a
  decommissioned worker doesn't present as a dark host.

One-off remediation for caspar: DONE 2026-08-14 (bootout + disable + plist
moved to `.removed-20260814`, stale `host_heartbeat` row deleted). What
remains is making the playbook do this itself so the next `data` host —
or a caspar reprovision — doesn't regress.
