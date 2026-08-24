# Runbook — macOS FDA-grant drift audit

**Cadence:** 30 days (advisory nudge: `scripts/fda-grant-review`, surfaced in
`/whatneedsdoing`). The `## Log` section below is the clock.

## Why this exists

macOS 15+ (Sequoia / Tahoe-26) denies NFS access to **every launchd- or
cron-spawned process**, regardless of UID — only interactive SSH/GUI sessions
get implicit access. So each cluster daemon that touches the NAS
(`/opt/nas/botshome`) needs **Full Disk Access** granted to its venv
interpreter. That is the 2026-07 gateway lockout — watch + worker dark for
days with no monitor (the backlog item "gateway daemon NAS lockout").

Historically macOS pinned that grant to the binary's **cdhash** — every
Homebrew python is ad-hoc signed, so `brew upgrade python@3.12|@3.14`
silently minted a fresh cdhash and invalidated every grant, EPERM-ing every
daemon on the NAS. **2026-08-24: migrated every NAS-touching venv to
python.org's macOS framework interpreters** (Developer-ID signed by the
Python Software Foundation, `roles/pythonorg`). TCC now stores a
**signature-based** requirement instead of a cdhash, so the grant survives a
python upgrade within the series (3.14.7 -> 3.14.8 keeps the same
TeamIdentifier) — one grant per series per host, forever. The cdhash-pinning
failure mode described above only ever afflicted Homebrew's ad-hoc-signed
builds; it cannot recur on python.org's Developer-ID-signed ones.

Two guards exist; this runbook is the **second**:

1. **Real-time backstop** — the nursery `nas-denied` detector
   (`_detect_nas_denied`, `src/precis/workers/nursery.py`). The `precis
   heartbeat` reporter probes `/opt/nas` from its own launchd context each tick
   and records `host_heartbeat.meta.nas_ok`; a fresh `false` raises a
   **critical** alert per host (auto-resolves when it flips back). Catches an
   *actual* lockout in minutes.
2. **Proactive drift audit (this pass)** — a slow monthly check that the
   *currently-resolved* daemon interpreters on each Mac are still on
   python.org (Developer-ID signed) and still **granted**, catching drift (a
   new venv left symlinked to a stale interpreter, a hand-copied binary, an
   OS re-sign) *before* it causes a lockout.

## Canonical binary list

The authority is `deploy/roles/tcc_profile/defaults/main.yml`
(`tcc_profile_binaries`). The `tcc_profile` role also renders a per-host
checklist to `/Users/Shared/cluster-fda-checklist.txt` on each `scripts/deploy`
(re-run the role if that file is stale — it omitted `/opt/precis/venv/bin/python3`
as of 2026-08-01). The NAS-touching interpreters in practice:
`/opt/precis/venv/bin/python3`, `/opt/precis/embedder-venv/bin/python3`,
`/opt/mcps/venv/bin/python3`, `/opt/mcps/extract/venv/bin/python3`,
`/opt/hermes/venv/bin/python3` (each present only on some hosts; absent ones are
skipped). Runs on every macOS host in the fleet; Linux hosts are immune, never
audit them.

## Audit procedure (when DUE)

For each macOS host, three checks:

  (a) **Every resolved venv interpreter is under python.org's framework
      path and Developer-ID signed** — `readlink -f` each venv python; the
      resolved path must start with
      `/Library/Frameworks/Python.framework/Versions/`, and
      `codesign -dv --verbose=2 <resolved path>` must show a
      `TeamIdentifier=` line that is not `not set`.
  (b) **Grants are present** — each resolved path shows up in
      `kTCCServiceSystemPolicyAllFiles` with `auth_value = 2`.
  (c) **Heartbeat is healthy** — `host_heartbeat.meta.nas_ok = true` for the
      host.

```bash
for v in /opt/precis/venv/bin/python3 /opt/precis/embedder-venv/bin/python3 \
         /opt/mcps/venv/bin/python3 /opt/mcps/extract/venv/bin/python3 \
         /opt/hermes/venv/bin/python3; do
  [ -e "$v" ] || continue
  resolved=$(readlink -f "$v")
  printf "  %s -> %s\n" "$v" "$resolved"
  codesign -dv --verbose=2 "$resolved" 2>&1 | grep TeamIdentifier
done
sudo sqlite3 "/Library/Application Support/com.apple.TCC/TCC.db" \
  "SELECT client, auth_value FROM access WHERE service=\"kTCCServiceSystemPolicyAllFiles\" AND client LIKE \"%python%\";"
```

```bash
scripts/prod-psql "SELECT host, meta->>'nas_ok' AS nas_ok FROM host_heartbeat;"
```

### Remediate any drift

- **Interpreter not under `/Library/Frameworks`, or `TeamIdentifier=not set`**:
  the venv rebuilt against a stale symlink somehow escaped python.org — re-run
  the owning role (`mcps`, `extract_watch`, `precis_worker`, or
  `precis_embedder`); its venv-mode detection wipes and rebuilds against the
  python.org interpreter automatically.
- **Ungranted resolved interpreter** (path with `auth_value=0`, or absent): grant
  it in **System Settings → Privacy & Security → Full Disk Access → `+` → ⌘⇧G**,
  paste the resolved real path, toggle **ON**. TCC can't be set from the CLI
  (SIP-protected; unsigned MDM/PPPC profiles are rejected on macOS 26).
- **After granting**, restart the daemons so they re-exec under the new grant
  (TCC is evaluated at launch): `sudo launchctl kickstart -k system/com.precis.watch`
  (and `com.precis.worker`, etc.). A grant added while a daemon runs is NOT
  adopted until restart.

Then append a dated line to the `## Log` below.

## Notes / gotchas

- `timeout` is absent on macOS by default — don't wrap `ssh` in it (use
  `gtimeout` from coreutils if you must, or just let it run).
- `sudo -u deploy` and interactive SSH sessions get implicit NAS access, so they
  can NEVER reproduce the daemon-context denial — verify with the heartbeat
  `nas_ok` or a throwaway `UserName=deploy` LaunchDaemon, not a login shell.
- Any `brew pin python@3.x` markers left over from before the python.org
  migration are vestigial and may be removed — nothing reads them anymore.

## Log

- **2026-08-01** — Initial pass. gateway's FDA grant had broken on a
  `brew upgrade python` (`3.12.13`→`3.12.13_4`, `3.14.3_1`→`3.14.6`); re-granted
  both current interpreters, kickstarted watch/worker/heartbeat, verified
  `nas_ok=true` + nursery alert 180213 auto-resolved. The other two Macs were
  healthy (older, still-granted builds). All three Macs `brew pin`ned
  python@3.12 + python@3.14 at their current builds. Detector
  (`nas-denied`) + this cadence shipped `f0d16c22`.
- **2026-08-24** — Migrated all NAS-touching venvs to python.org interpreters
  (Developer-ID signed); FDA grants no longer break on python upgrades.
  Trigger: gateway web daemon lockout after an unpinned brew upgrade of
  python@3.14 (3.14.6→3.14.7).
