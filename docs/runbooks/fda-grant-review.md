# Runbook — macOS FDA-grant + brew-pin drift audit

**Cadence:** 30 days (advisory nudge: `scripts/fda-grant-review`, surfaced in
`/whatneedsdoing`). The `## Log` section below is the clock.

## Why this exists

macOS 15+ (Sequoia / Tahoe-26) denies NFS access to **every launchd- or
cron-spawned process**, regardless of UID — only interactive SSH/GUI sessions
get implicit access. So each cluster daemon that touches the NAS
(`/opt/nas/botshome`) needs **Full Disk Access** granted to its venv
interpreter. macOS pins that grant to the binary's **cdhash**, so
`brew upgrade python@3.12|@3.14` silently invalidates it and every daemon
starts `EPERM`-ing on the NAS. That is the 2026-07 melchior lockout — watch +
worker dark for days with no monitor (`OPEN-ITEMS` "melchior daemon NAS
lockout").

Two guards now exist; this runbook is the **second**:

1. **Real-time backstop** — the nursery `nas-denied` detector
   (`_detect_nas_denied`, `src/precis/workers/nursery.py`). The `precis
   heartbeat` reporter probes `/opt/nas` from its own launchd context each tick
   and records `host_heartbeat.meta.nas_ok`; a fresh `false` raises a
   **critical** alert per host (auto-resolves when it flips back). Catches an
   *actual* lockout in minutes.
2. **Proactive drift audit (this pass)** — a slow monthly check that the
   *currently-resolved* daemon interpreters on each Mac are still **granted**
   and **pinned**, catching drift (a new venv on an ungranted interpreter, a
   manual `brew unpin` + upgrade, an OS re-sign) *before* it causes a lockout.

Prevention that makes drift rare: all Macs are `brew pin`ned
(`brew pin python@3.12 python@3.14`) so an unattended `brew upgrade` can't move
the cdhash. Unpin deliberately (`brew unpin`) only when you intend to upgrade —
and re-grant + re-pin as part of that.

## Canonical binary list

The authority is `deploy/roles/tcc_profile/defaults/main.yml`
(`tcc_profile_binaries`). The `tcc_profile` role also renders a per-host
checklist to `/Users/Shared/cluster-fda-checklist.txt` on each `scripts/deploy`
(re-run the role if that file is stale — it omitted `/opt/precis/venv/bin/python3`
as of 2026-08-01). The NAS-touching interpreters in practice:
`/opt/precis/venv/bin/python3`, `/opt/precis/embedder-venv/bin/python3`,
`/opt/mcps/venv/bin/python3`, `/opt/mcps/extract/venv/bin/python3`,
`/opt/hermes/venv/bin/python3` (each present only on some hosts; absent ones are
skipped). Macs: **melchior, balthazar, caspar**. **spark is Linux → immune**,
never audit it.

## Audit procedure (when DUE)

For each Mac, resolve every venv interpreter and check its TCC grant + pin.
`readlink -f` gives the cdhash-bearing real path; the grant must match *that*
path with `auth_value = 2`.

```bash
for h in melchior balthazar caspar; do
  echo "######## $h ########"
  ssh "$h" '
    echo "-- resolved interpreters (what daemons run) --"
    for v in /opt/precis/venv/bin/python3 /opt/precis/embedder-venv/bin/python3 \
             /opt/mcps/venv/bin/python3 /opt/mcps/extract/venv/bin/python3 \
             /opt/hermes/venv/bin/python3; do
      [ -e "$v" ] && printf "  %s -> %s\n" "$v" "$(readlink -f "$v")"
    done
    echo "-- FDA grants (2=granted 0=NOT) --"
    sudo sqlite3 "/Library/Application Support/com.apple.TCC/TCC.db" \
      "SELECT client, auth_value FROM access WHERE service=\"kTCCServiceSystemPolicyAllFiles\" AND client LIKE \"%python%\";"
    echo "-- brew pin markers (should point at CURRENT builds) --"
    ls -la "$(/opt/homebrew/bin/brew --prefix)/var/homebrew/pinned" 2>&1 | grep -i python
  '
done
```

Cross-check, per host: **every resolved real path** appears in the grants with
`auth_value = 2`, and each `python@3.1x` pin marker points at that *same*
current build. Also confirm live health:

```bash
scripts/prod-psql "SELECT host, meta->>'nas_ok' AS nas_ok FROM host_heartbeat WHERE host IN ('melchior','balthazar','caspar');"
```

### Remediate any drift

- **Ungranted resolved interpreter** (path with `auth_value=0`, or absent): grant
  it in **System Settings → Privacy & Security → Full Disk Access → `+` → ⌘⇧G**,
  paste the resolved real path, toggle **ON**. TCC can't be set from the CLI
  (SIP-protected; unsigned MDM/PPPC profiles are rejected on macOS 26).
- **After granting**, restart the daemons so they re-exec under the new grant
  (TCC is evaluated at launch): `sudo launchctl kickstart -k system/com.precis.watch`
  (and `com.precis.worker`, etc.). A grant added while a daemon runs is NOT
  adopted until restart.
- **Unpinned python**: `brew pin python@3.12 python@3.14` on that host.

Then append a dated line to the `## Log` below.

## Notes / gotchas

- `timeout` is absent on macOS by default — don't wrap `ssh`/`brew` in it (use
  `gtimeout` from coreutils if you must, or just let it run).
- `brew list --pinned` can be slow (first call fetches the JSON API); the pin
  markers under `.../var/homebrew/pinned/` are the fast, reliable truth.
- `sudo -u deploy` and interactive SSH sessions get implicit NAS access, so they
  can NEVER reproduce the daemon-context denial — verify with the heartbeat
  `nas_ok` or a throwaway `UserName=deploy` LaunchDaemon, not a login shell.

## Log

- **2026-08-01** — Initial pass. melchior's FDA grant had broken on a
  `brew upgrade python` (`3.12.13`→`3.12.13_4`, `3.14.3_1`→`3.14.6`); re-granted
  both current interpreters, kickstarted watch/worker/heartbeat, verified
  `nas_ok=true` + nursery alert 180213 auto-resolved. balthazar + caspar were
  healthy (older, still-granted builds). All three Macs `brew pin`ned
  python@3.12 + python@3.14 at their current builds. Detector
  (`nas-denied`) + this cadence shipped `f0d16c22`.
