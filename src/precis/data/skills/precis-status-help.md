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
**Build** (version, git sha, dirty flag, last release tag, build
time/host/user), **Runtime** (container hostname, python, pid,
uptime), **Database** (connected DSN host/port/name/user, postgres
server version, last applied migration + count), and the existing
**Optional dependencies** import probe.

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
section. It surfaces both `version` (from `precis.__version__` —
the hard-coded literal in `src/precis/__init__.py`) and
`git_last_tag` (the latest git tag reachable from the build's HEAD,
e.g. `v8.4.4`). Those two can drift — the literal isn't always
bumped in lockstep with release tags — and seeing them side by side
is the point. When the env vars weren't baked in at image build
(e.g. a bare `docker build .` or a plain `pip install`), the
git-derived fields render as `unknown`; the `version` field always
populates.

## What git commit am I on?
## What git sha is this build from?
## How do I see the git hash of this container?
## Is this build clean or dirty?
## Is the working tree dirty in this build?
## How do I check the git dirty status?

Same call: `get(kind='skill', id='precis-status')`. The **Build**
section reports `git_sha`, `git_sha_short`, `git_dirty` (`0` clean,
`1` modifications present at build time), `git_describe`
(`v8.4.4-12-gabc123-dirty` style), and `git_branch`. These come
from env vars baked into the image by `scripts/build-image`; a bare
`docker build` produces `unknown` for all of them, which is itself
a useful signal that the image wasn't built through the wrapper.

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
your venv is on), `pid`, `started_at` (process start, captured at
module import), and `uptime_seconds`. Useful when a restart loop is
suspected and you want to confirm "yes, this process is fresh".

## Is my build out of date?
## How do I check for stale builds?
## How do I know if I need to rebuild?

Cross-reference three fields in the **Build** section:

- `version` vs `git_last_tag` — a gap (e.g. `8.1.0` vs `v8.4.4`)
  means `__version__` in `src/precis/__init__.py` hasn't been
  bumped in lockstep with the release tags. The image is current
  with the tag, just the literal lags.
- `git_dirty` — `1` means the image was built with uncommitted
  changes on the build host. Fine for dev iteration; surprising in
  prod.
- `build_time` — how stale is this image? Compare against your
  most recent merge to `main`.

When you do need to rebuild with fresh metadata, run
`scripts/build-image` from the precis-mcp repo root. It captures
the current git facts on the host and threads them through
`docker compose build --build-arg` — the response then reflects
the new sha / tag / dirty flag on the next call to this skill.

## See also

- `get(kind='skill', id='precis-overview')` — orientation: seven
  verbs, one address scheme.
- `get(kind='skill', id='precis-help')` — active kinds + verbs on
  this server (from the live hub).
