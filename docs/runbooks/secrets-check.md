# Runbook — secrets-check (weekly external-credential check)

A **weekly** pass that asks: *does every external API credential the
product relies on still work?* An expired or revoked key otherwise shows up
only when a job fails (`pourbaix_bulk` with `failure_class=config`,
gr496305). `scripts/secrets-check-review` prints `DUE` when the newest dated
line in `## Log` is more than 7 days old; `/whatneedsdoing` surfaces it. The
script only keeps the clock — it never probes anything.

## The pass

1. `scripts/prod-precis secret check`. It runs the `/secrets` page's probes
   (`precis_web.secret_status._PROBES`: fixed provider endpoints, nothing
   else) against the prod vault and prints one line per registry secret:
   - `ok` — a probe authenticated.
   - `failed` — the provider rejected it; the line carries the HTTP status
     and the provider's key URL.
   - `missing` — not in the vault, env or file. `(optional)` ones need no
     action.
   - `unprobed` — no cheap auth-only check, or the probe was inconclusive
     (timeout, rate limit). Where jobs depend on the secret the line names
     the last succeeded job (`get(kind='job', id=N)`); that is evidence the
     key worked then, not a verification.
2. Each `failed` or non-optional `missing` credential becomes one
   `waiting-for:reto` todo (skill `precis-user-queue-help`) carrying the key
   URL from the line. Never paste a value anywhere.
3. Append one dated line to `## Log` (newest first) with the four counts and
   the queued todo ids. That resets the clock.

## Notes

- The command resolves secrets as the service does: process env first, then
  the vault. Run it through `scripts/prod-precis` from a shell that exports
  no provider keys, or an exported key is the one that gets probed.
- Each vault read writes the usual `vault.reveal` audit row.
- The check never prints, logs or stores a secret value, and adding a probe
  means adding an entry to `_PROBES` with a fixed URL, never a configurable one.
- Registry completeness is a test:
  `tests/precis_web/test_secret_registry_complete.py` fails when the code
  reads a secret name that has no `SecretSpec`.
- The scheduled-job variant (no human has to run the pass) is the backlog item
  `credential-check-scheduled-job`.

## Log

- **2026-10-18** — cadence established (this runbook, `scripts/secrets-check-review`, `precis secret check`). First pass due.
