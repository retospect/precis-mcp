# Restart worker and watch

The `worker` and `watch` daemons are a pair (`com.precis.worker` /
`com.precis.watch` on launchd, `precis-worker` / `precis-watch` on systemd).
Restarting only `watch` leaves `worker` stopped, so the derived queue backlog
grows while the `watch` pass has nothing to pull. Always restart both in a
single command:

```bash
scripts/restart-worker-and-watch
```

If the plists/units need root (the usual cluster setup), run with `sudo`:

```bash
sudo scripts/restart-worker-and-watch
```

## OS-agnostic (gr180078)

The script detects which init system the host runs and picks the matching
verb — no manual branching needed:

- **launchd** (macOS cluster nodes): `launchctl kickstart -k <label>` — kills
  and immediately relaunches an already-loaded service.
- **systemd** (the Linux/GPU node): `systemctl restart <unit>`.

It restarts `worker` first, then `watch`. If a service is not loaded/present,
the script prints a warning and exits non-zero for that service.

## Why this exists

See the related `docs/backlog/` items.
