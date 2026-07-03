---
id: precis-status-help
title: precis — what version am I, what DB, what build?
summary: runtime introspection — build version, container, DB connection, migration state, dependencies
applies-to: precis-status (synthesised skill)
status: active
---

# precis-status-help — see your build, runtime, and DB at a glance

The `precis-status` synthesised skill answers the questions an
agent or operator asks when they're not sure what container, build,
or database they're talking to. One call returns four sections:
**Build** (version, git sha, branch, dirty flag, last release tag,
`git_source` + `source_path` provenance, build time/host/user),
**Runtime** (container hostname, python, pid, cwd, uptime),
**Database** (connected DSN host/port/name/user, postgres server
version, last applied migration + count), and the existing
**Optional dependencies** import probe.

The git facts come from one of two lanes, shown by the `git_source`
field: `image-build` (baked into a Docker image by
`scripts/build-image`) or `working-tree` (read from the live checkout
the code loaded from — local dev, an editable install, or the
cluster's from-git checkout). Either way the values are **frozen at
process start**, so they tell you what *this running process* loaded,
not what the checkout says right now.

To see it:

```python
get(kind='skill', id='precis-status')
```

That's it — no args, no setup. The rest of this skill is a search
ramp so a natural-language query for any of these intents lands
here.

## What version am I running?
## What version of precis-mcp is this?
## How do I check my precis-mcp version?
## How do I get the current precis version?
## What release am I on?
## What is my release tag?
## What's the precis-mcp release?
## How do I tell which precis build this is?

Call `get(kind='skill', id='precis-status')` and read the **Build**
section. It surfaces both `version` (from `precis.__version__`,
which now derives from the installed distribution metadata via
`importlib.metadata.version("precis-mcp")` — so it can no longer
drift from the packaged `pyproject.toml` version the way the old
hand-maintained literal did) and `git_last_tag` (the latest git tag
reachable from HEAD, e.g. `v8.4.4`). When neither the baked env vars
nor a live git checkout are available (a wheel in `site-packages`
with no `.git`, no `git` binary), the git-derived fields render as
`unknown`; the `version` field always populates.

## What git commit am I on?
## What git sha is this build from?
## How do I see the git hash of this container?
## Is this build clean or dirty?
## Is the working tree dirty in this build?
## How do I check the git dirty status?

Same call: `get(kind='skill', id='precis-status')`. The **Build**
section reports `git_sha`, `git_sha_short`, `git_dirty`,
`git_describe` (`v8.4.4-12-gabc123-dirty` style), `git_branch`, and
`source_path` (the on-disk checkout the process is running from).
The `git_source` field tells you the lane:

- `image-build` — baked into the image by `scripts/build-image` at
  `docker build` time (`git_dirty` reads `0`/`1`).
- `working-tree` — read from the live checkout at
  `source_path`, frozen when the process started (`git_dirty` reads
  `true`/`false`). This is what you get on a local run, an editable
  install, or a cluster node running from a `uv`/`pip`-from-git
  checkout.
- `unknown` — no git and no baked env vars (an installed wheel with
  no `.git`); all git fields render `unknown`.

## What database am I connected to?
## Which DB is this pointing at?
## What DSN is this using?
## What postgres server is this on?
## How do I see the connected database?
## What's the database host?
## How do I check what DB precis is using?

Call `get(kind='skill', id='precis-status')` and read the
**Database** section. Fields: `dsn_host` and `dsn_port` (parsed
from `PRECIS_DATABASE_URL` — password is never echoed back), `name`
(`SELECT current_database()`), `user` (`SELECT current_user`), and
`server_version` (`SELECT version()`). When the DB is unreachable,
the section renders `unreachable: <ExcType>: <msg>` inline rather
than crashing the whole status call — this surface is the first
thing you hit *because* something is wrong, so it stays usable when
the DB is the thing wrong.

## What migration version is the DB at?
## What schema version is this?
## What's the latest applied migration?
## How do I check the migration version?
## How do I see which migrations have run?
## How do I tell what schema version is deployed?

Same call. The **Database** section reports `migration` (the
`version` value of the highest-version row in `public._migrations`,
e.g. `0005_gripe_first_class_and_jobs`) and `migration_count` (the
total number of applied migrations). Use these to confirm the
schema state matches what your branch expects before you start
debugging "why doesn't this column exist?".

## What container am I running in?
## What's the container hostname?
## How long has this process been up?
## What's the process pid?
## What python version is this build using?
## How do I check the runtime info?

Same call. The **Runtime** section reports `hostname`
(`socket.gethostname()` — the container's name, not the host's),
`platform` (`platform.platform()`), `python` (the major.minor.patch
your venv is on), `pid`, `cwd` (the process working directory),
`started_at` (process start, captured at module import), and
`uptime_seconds`. Useful when a restart loop is suspected and you
want to confirm "yes, this process is fresh".

## Is my build out of date?
## Am I running the current code or an old one?
## Am I N commits behind — do I need to restart to refresh?
## How do I check for stale builds?
## How do I know if I need to rebuild?

The `git_sha` in the **Build** section is **frozen at the moment the
process started** — it is what *this running process* loaded, not
what the checkout on disk says now. That is exactly the signal you
want: to tell whether a long-running server/worker is behind the
code, compare its reported `git_sha` against the tip of the branch:

```bash
git -C <source_path> rev-parse HEAD    # what the checkout is at now
```

If they differ, the checkout moved ahead (a `git pull`, a ship, a
redeploy) **but the process never restarted** — it is still running
the old sha and needs a restart to pick up the new code. (A naive
on-demand `git rev-parse` would read the fresh sha and falsely report
"current"; freezing at startup is what makes the drift visible.)

Other fields to cross-reference:

- `git_source` — `working-tree` means a live checkout you can diff as
  above; `image-build` means a baked image, so compare against the
  image you expect to be deployed.
- `version` vs `git_last_tag` — with `version` now sourced from the
  installed distribution metadata, a gap here means the checkout is
  between releases, not that a literal lagged.
- `git_dirty` — uncommitted changes were present when the process
  loaded. Fine for dev iteration; surprising in prod.
- `build_time` (image builds) — how stale is this image? Compare
  against your most recent merge to `main`.

For a Docker image, rebuild fresh metadata with `scripts/build-image`
from the repo root; for a from-source run, restart the process after
updating the checkout.

The same one-liner is logged to stderr at server boot
(`precis-mcp <version> @ <sha> (<branch>) [<git_source>] <path>`), so
you can also read it straight from the process log.

## See also

- `get(kind='skill', id='precis-overview')` — orientation: seven
  verbs, one address scheme.
- `get(kind='skill', id='precis-help')` — active kinds + verbs on
  this server (from the live hub).
