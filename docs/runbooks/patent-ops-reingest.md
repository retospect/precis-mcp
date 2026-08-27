# Run a patent OPS job by hand (`precis jobs reingest-patents`)

Running a patent OPS job manually on melchior (e.g. the force-reingest
backfill `precis jobs reingest-patents`) has three traps that each cost
hours to diagnose:

1. **Run as `hermes`, not deploy or your ssh user.** The EPO OPS client's
   dogpile fetch cache lives at a **fixed** path
   `/var/tmp/python-epo-ops-client/` (`cache.dbm` + `throttle_history.db`),
   and that dir is `hermes`-owned mode 755. Other users can't write it →
   `OperationalError: attempt to write a readonly database`. `sudo -u hermes`
   works (passwordless sudo is on).

2. **`cache.dbm` "db type could not be determined" = Python-version dbm
   mismatch.** The dogpile cache is a SQLite file if a Python 3.13 `dbm`
   created it (`dbm.sqlite3`); the deployed `/opt/precis/venv` is 3.12, whose
   `dbm` can't recognise it → every OPS call throws
   `dbm.error: db type could not be determined`. Fix:
   `sudo rm -f /var/tmp/python-epo-ops-client/cache.dbm*` — it's a
   disposable fetch cache; dogpile recreates a compatible one. (cwd is
   irrelevant — the cache path is absolute.)

3. **Point `PRECIS_PATENT_RAW_ROOT` at a writable temp dir** (e.g.
   `/var/tmp/reingest-raw`) for ad-hoc runs. Patent XML on disk is
   forensic-only — `parse_patent` works off the in-memory fetched bytes —
   so overriding the NAS root (`/opt/nas/botshome/patents-raw`, an autofs
   NFS mount) avoids any mount/permission rabbit hole with zero loss.

## Recipe

Env comes from the watch daemon's plist (secrets never printed):

```python
# pipe via: ssh melchior 'sudo -u hermes /opt/precis/venv/bin/python3' < script.py
import plistlib, os, subprocess
d = plistlib.load(open("/Library/LaunchDaemons/com.precis.watch.plist", "rb"))
env = dict(os.environ); env.update(d["EnvironmentVariables"])   # EPO creds + DSN
env["PRECIS_PATENT_RAW_ROOT"] = "/var/tmp/reingest-raw"
subprocess.run(["/opt/precis/venv/bin/precis", "jobs", "reingest-patents"], env=env)
```

`com.precis.watch.plist` (UserName=deploy, WorkingDirectory=/tmp) carries
`EPO_OPS_CLIENT_KEY/SECRET`, `PRECIS_DATABASE_URL`,
`PRECIS_PATENT_RAW_ROOT`. `worker.plist` has the DSN but **not** the EPO
creds.

## Coverage facts

OPS DOCDB serves separate description/claims full text mainly for
**EP/WO/US**; **CN/KR/JP** bodies come from the patents.google.com fallback
(`fetch_google_patents`, `PRECIS_GP_FETCH=1`), stored as per-claim
`chunk_kind='patent_claim'` chunks (vs OPS's `meta.patent_block='claim'`).
A force-reingest must **not** clobber a google-populated ref when OPS
returns empty (guarded in `ingest_patent` since 2026-07-16).
