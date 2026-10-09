---
id: precis-status-help
title: precis — what version am I, what DB, what build?
summary: runtime introspection — build version, container, DB connection, migration state, dependencies
answers:
  - how do I check what version this build is running?
  - what git commit is this server actually on?
  - how do I check what database precis is connected to?
  - how do I tell if my build is out of date and needs a restart?
  - is the running server the same code as the checkout in front of me?
  - what columns does a table have / where is the database schema written down?
applies-to: precis-status (synthesised skill)
status: active
tags: orientation, troubleshooting
kinds: skill
---

# precis-status-help — see your build, runtime, and DB at a glance

The `precis-status` synthesised skill answers the questions an
agent or operator asks when they're not sure what container, build,
or database they're talking to. One call returns four sections:
**Build** (`served_sha` + `served_from` — the commit this process
executes and where it came from — `image_sha`, git branch, dirty flag,
last release tag, `source_path`, `source_drift`, build time + `build_age`,
host/user, then one `⚠ WARN` line per staleness finding),
**Runtime** (container hostname, python, pid, cwd, uptime, and
`md_vector_warmup` when the `md` kind is registered, which reads
`warming: batch i/N (k new)` while the pass runs, then one of
`warm (k new)`, `warm with gaps (k new, m batch(es) skipped)`, or
`COLD`. ⚠ `COLD` means a NON-retryable error — a dim mismatch or a bad
text. An unreachable or saturated embedder reports *gaps*, because a
retryable failure skips its batch and the pass carries on; a gapped pass
also counts as a failure for the re-arm backoff. No row at all means the
`md` kind is registered without an embedder, not that the cache is
cold),
**Database** (connected DSN host/port/name/user, postgres server
version, last applied migration + count), and the existing
**Optional dependencies** import probe.

## Where the git facts come from: the five `served_from` lanes

The git facts come from one of five lanes, shown by `served_from`
(`git_source` is the same value under its older name):
`watched-checkout` (the HEAD of the tree `PRECIS_CHECKOUT_WATCHDOG`
names — the shared session server, which imports from a `.git`-less
snapshot of that tree), `image+mount` (a live checkout mounted over an
image's source — the per-session dev container; the served files are
the mount's, and the image's own commit is shown separately as
`image_sha`), `working-tree` (read from the live checkout the code
loaded from — local dev or an editable install), `image-build` (baked
into a Docker image by `scripts/build-image`, nothing mounted), or
`vcs-install` (recovered from the installed wheel's `direct_url.json`
when the package was `pip`/`uv`-installed straight from a git URL —
the cluster's `… @main` venv, or a git-sourced image). Whichever lane
answers, the values are **frozen at process start**, so they tell you
what *this running process* loaded, not what the checkout says right
now. A baked `image_sha` never outranks a mounted checkout: that
masking is how a three-week-old image sha was reported for a tree that
was current to the minute (gr458061).

**One lane answers every git-identity field.** A field the winning lane
cannot supply renders `unknown` rather than being filled in from the next
lane down. This matters: a `git_dirty` from the image build sitting beside
a sha from somewhere else reads as "clean and verified" and makes a wrong
sha look corroborated (gr457361). `build_time`/`build_host`/`build_user`
are the exception — they describe the image build, which is a separate
question, and are always read from the baked env.

## Is my MCP stale? `source_drift`, the WARN lines, and a missing row

`source_drift` is the one field read *live* rather than frozen, and it is
what answers "is my MCP stale?": `none` (the served tree is still at the
sha this process imported), `moved <old>→<new>` (it advanced; this process
is serving code the tree no longer has — restart it), or `unknown` (no
tree to compare, so **nothing was checked** — not a claim of freshness).
The served tree is the watched checkout when one is named, else the
checkout the code imported from (the local run, the editable install,
the bind-mounted dev container).

Anything short of `none` is also said out loud as a `⚠ WARN` line under
the Build table, and any WARN makes the verdict `Overall: WARN` instead
of `OK`: `WARN staleness unknown — <what could not be compared>`,
`WARN served tree is N commits behind the mounted checkout (<old>→<new>)`
(N from git when it can read the tree; "behind" without a count when it
cannot), and `WARN build is <age> old` when `build_time` is older than
`PRECIS_BUILD_AGE_WARN_DAYS` (default 3). `build_age` renders the age
beside the timestamp so nobody has to date-subtract by hand.

**A row missing entirely is the oldest signal of all**: a process that
started before `source_drift` (or `served_sha`) existed renders no such
row and no WARN. Absence means the process is older than the field, so
it is stale by definition and cannot tell you by how much. Do not read a
missing row as `none`. Reconnect (`/mcp` → the server → reconnect) to
land on the shared server.

A bare `docker build` that skips `scripts/build-image` (so no
`--build-arg` git values are passed) does **not** count as
`image-build`: the Dockerfile defaults those args to the literal
`unknown`, and the status builder treats an `unknown`/blank baked
value as absent — falling through to the `vcs-install` or `unknown`
lane rather than falsely claiming a baked identity it doesn't have.

To see it:

```python
get(kind="skill", id="precis-status")
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

- `watched-checkout` — the resolved HEAD of the tree
  `PRECIS_CHECKOUT_WATCHDOG` names, read from its `.git` at process start.
  Outranks everything else because it is the only lane that can be right
  when the code executes from a *copy* of a tree: the shared session
  server's entrypoint snapshots the bind-mounted `/src` into `/app`
  excluding `.git`, so `import precis` resolves somewhere with no git at
  all and the image's baked sha describes a venv built weeks earlier.
  `git_dirty`, `git_describe` and `git_last_tag` stay `unknown` — the
  reader parses `.git` directly (it must: the tree is usually a read-only
  mount owned by another uid, where `git` refuses to run) and those three
  need real git.
- `image+mount` — a live checkout under the import path *and* a real
  baked sha: the per-session dev container, whose `/app` is a bind mount
  of the host checkout. The git fields are the mount's; `image_sha` is
  the image's. Where git refuses the mount (read-only, foreign uid) the
  `.git` is parsed directly, so `git_dirty`/`git_describe`/`git_last_tag`
  may read `unknown` while the sha and branch are right.
- `working-tree` — read from the live checkout at
  `source_path`, frozen when the process started (`git_dirty` reads
  `true`/`false`). This is what you get on a local run or an editable
  install.
- `image-build` — baked into the image by `scripts/build-image` at
  `docker build` time (`git_dirty` reads `0`/`1`), with no checkout
  anywhere under the import path. Requires *real* build-args: an image
  built without them (the Dockerfile's `unknown` default) is treated as
  absent and falls through below. Nothing can be compared here, so the
  page carries `WARN staleness unknown`; `build_age` is the signal.
- `vcs-install` — recovered from the installed wheel's
  `direct_url.json` (`vcs_info.commit_id` + `requested_revision`), for
  a `pip`/`uv` install straight from a git URL: a cluster node running
  the `… @main` venv, or a git-sourced image. No `.git` and no
  build-args, but the resolved commit is still known. `git_dirty` and
  `git_describe` stay `unknown` (metadata records neither).
- `unknown` — no git, no real baked env vars, and no VCS metadata (an
  installed wheel from a local `pip install .`); all git fields render
  `unknown`.

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

Read the `⚠ WARN` lines under **Build** and `Overall:` at the bottom.
`Overall: OK` now means every staleness check ran and passed; anything
the page could not verify is a WARN, never silence. Then `source_drift`,
which does the comparison below for you and on the shared session server
is the only check that works:

- `none` — the served tree is still at the sha this process imported.
- `moved <old>→<new>` — the tree advanced and the process never
  restarted. It is serving the old code; the WARN says how many commits
  behind. Restart it.
- `unknown` — no tree to compare, so nothing was checked; the WARN names
  why. Fall back to the manual check and to `build_age`.
- *no `source_drift` row at all* — the process predates the field, so it
  is stale by definition. Reconnect rather than measuring; a process that
  cannot report drift also cannot tell you how far it has drifted.

`served_sha` in the **Build** section is **frozen at the moment the
process started** — it is what *this running process* loaded, not
what the checkout on disk says now, and not the image's `image_sha`.
That is exactly the signal you want: to tell whether a long-running
server/worker is behind the code, compare its `served_sha` against the
tip of the branch:

```bash
git -C <source_path> rev-parse HEAD    # what the checkout is at now
```

If they differ, the checkout moved ahead (a `git pull`, a ship, a
redeploy) **but the process never restarted** — it is still running
the old sha and needs a restart to pick up the new code. (A naive
on-demand `git rev-parse` would read the fresh sha and falsely report
"current"; freezing at startup is what makes the drift visible.)

**Do not substitute a cheap check from inside the container.** A `stat`,
a `grep` of a source file, and a fresh `python -c "import precis"` all
read the *files*, which a bind mount keeps current while the process
keeps serving the modules it imported hours ago. In one measured incident
all three agreed that the code was current, two detailed root-cause
analyses were written on that basis, and both were wrong (gr458061).
`source_drift` compares a sha frozen at import against the tree now, so
it cannot agree by construction.

Other fields to cross-reference:

- `served_from` — `working-tree`/`image+mount` mean a live checkout you
  can diff as above; `image-build` means a baked image, so compare
  against the image you expect to be deployed.
- `version` vs `git_last_tag` — with `version` now sourced from the
  installed distribution metadata, a gap here means the checkout is
  between releases, not that a literal lagged.
- `git_dirty` — uncommitted changes were present when the process
  loaded. Fine for dev iteration; surprising in prod.
- `build_age` (image builds) — how stale is this image, as an age;
  past 3 days it is already a WARN. Compare against your most recent
  merge to `main`.

For a Docker image, rebuild fresh metadata with `scripts/build-image`
from the repo root; for a from-source run, restart the process after
updating the checkout.

The same one-liner is logged to stderr at server boot
(`precis-mcp <version> @ <sha> (<branch>) [<served_from>] <path>`), so
you can also read it straight from the process log.

## Is the running server the code in this directory?
## Are you running the precis-mcp in this repo / worktree?
## Does the connected MCP match my current checkout?
## What git hash is the live MCP server actually on?
## Are my edits live in the running server?

This section answers **build staleness** — which code a process runs.
It is not a liveness or health check: for "is X healthy / up?" read
`get(kind='alert', id='/health')` first ([[precis-alert-help]] §Health
questions). Lazily-loaded services idle-unload, so the `ps` step below
finding no process says nothing about health.

Reconcile the *connected* server against the checkout in front of you:

1. **Boot facts** — `get(kind='skill', id='precis-status')`, read
   **Build**: `served_sha`, `git_branch`, `served_from`, `source_path`
   (frozen at process start) and any `⚠ WARN` line.
2. **Map `source_path` to a host dir** — `source_path`/`cwd` are
   container-internal (a bare `/app`). For a Dockerized server, find the
   bind mount on the host:
   ```bash
   ps aux | grep -Ei 'precis.*(serve|mcp)' | grep -v grep
   # -v <HOST_PATH>:/app:ro  → HOST_PATH is the real checkout
   ```
3. **Compare** — `git -C <HOST_PATH> rev-parse HEAD` (and
   `--abbrev-ref HEAD`) vs your worktree's HEAD.

Reading it:

- **branch is `main`, not `worktree-<name>`** → it's the main tree, not
  your worktree; matching shas just mean you haven't committed yet (they
  diverge on first commit — the frozen server never picks up worktree
  commits).
- **server `served_sha` != HOST_PATH HEAD** → checkout moved, process
  never restarted → stale, restart to refresh.
- **mount path != your worktree** → different tree; a local dev MCP
  usually mounts the main repo `:ro` at prod, so worktree edits are
  invisible until you rebuild + restart pointed at the worktree.

## Where the database schema is written down

Before reaching for `information_schema` or `\d <table>`, read
`docs/reference/schema.md` — it is generated from the live DB by
`scripts/gen-schema` and lists every table with its columns and the ER
diagram. Agents re-derive this by hand constantly (a 5-day window held 96
`information_schema` queries across 21 sessions, several of them
rediscovering the same tables), and it is almost always already documented.

Two honest caveats, so the doc doesn't send you the wrong way:

- **Check the `Source: precis_prod @ <date>` line in its header first.** The
  doc is a snapshot, not a live view, and it has run months stale. If the
  date is old, treat it as a strong hint rather than truth.
- **If your table isn't listed, it may be newer than the snapshot** — that is
  the case where introspecting the live DB is the right move, not the
  fallback. Regenerate with `scripts/gen-schema` if you have a checkout.

Repo-side file, so this route needs a checkout; a prod-only agent still has
to introspect.

## See also

- [[precis-alert-help]] — health questions: `/health`, failure ids, why
  process presence is not health.
- [[precis-overview]] — orientation: seven verbs, one address scheme.
- [[precis-help]] — the synthesised skill listing active kinds + verbs
  on this server (from the live hub, not a file — still a valid
  wikilink target, resolved against the synth slug set).
