# Restart worker and watch

The `com.precis.worker` and `com.precis.watch` launchd daemons are a pair.
Restarting only `watch` leaves `worker` stopped, so the derived queue backlog
grows while the `watch` pass has nothing to pull. Always restart both in a
single command:

```bash
scripts/restart-worker-and-watch
```

If the plists are owned by `root` (the usual cluster setup), run with `sudo`:

```bash
sudo scripts/restart-worker-and-watch
```

The script restarts `worker` first, then `watch`, using `launchctl kickstart -k`
so each service is killed and immediately relaunched. If a service is not
loaded, the script prints a warning and exits non-zero for that service.

## Why this exists

See `OPEN-ITEMS.md` / "Architecture review / compaction / footguns".
