---
id: precis-status-help
family: core
title: precis — what version am I, what DB, what build?
summary: runtime introspection — build version, git commit served, staleness warnings, DB connection, migration state, dependencies
answers:
  - how do I check what version this build is running?
  - what git commit is this server actually on?
  - how do I check what database precis is connected to?
  - how do I tell if my build is out of date?
applies-to: precis-status (synthesised skill)
status: active
tags: orientation, troubleshooting
kinds: skill
---

# precis-status-help — see your build, runtime, and DB at a glance

The `precis-status` synthesised skill answers the questions an
agent asks when it is not sure which build, process, or database it
is talking to. One call returns these sections:
**Build** (`served_sha` + `served_from` — the commit this process
executes and where it came from — `image_sha`, git branch, dirty flag,
last release tag, `source_path`, `source_drift`, build time + `build_age`,
then one `⚠ WARN` line per staleness finding),
**Runtime** (hostname, python, pid, cwd, uptime,
`dispatch_slots` — the tool-call dispatch ceiling: `limit` slots, how many
are `in_flight`, how many calls are `queued` waiting, `refused_1h` (calls
refused unrun in the last hour after waiting `queue_wait_s` for a slot —
see "The server refused my call" below), and
`tool_calls_1h` + `tool_latency_1h <verb>/<kind>` (p50/p95 of the slowest
verb/kind pairs over the last hour, this process only), `md_vector_warmup`
when the `md` kind is registered, which reads
`warming: batch i/N (k new)` while the pass runs, then one of
`warm (k new)`, `warm with gaps (k new, m batch(es) skipped)`, or
`COLD`. ⚠ `COLD` means a NON-retryable error — a dim mismatch or a bad
text. An unreachable or saturated embedder reports *gaps*: the batch is
skipped and the pass carries on. No row at all means the
`md` kind is registered without an embedder, not that the cache is
cold),
**Database** (connected DSN host/port/name/user, postgres server
version, last applied migration + count, and `schema_state` —
ledger-vs-package drift), **Dark switches**, **Tool-call latency** (p50/p95
per verb/kind over the last hour, every process, with the p95 budget the
health digest alerts on), and **Optional dependencies**.

To see it:

```python
get(kind="skill", id="precis-status")
```

That's it — no args, no setup. The rest of this skill is a search
ramp so a natural-language query for any of these intents lands
here.

## Where the git facts come from: `served_from`

`served_from` names how the process got its git identity:
`watched-checkout` (the shared session server, reading the HEAD of the
tree it serves), `image+mount` (a live checkout mounted over an image's
source; the image's own commit is shown separately as `image_sha`),
`working-tree` (a live checkout the code loaded from), `image-build` (baked
into an image, nothing mounted) or `vcs-install` (recovered from the
installed package's install metadata). Whichever answers, the values are
**frozen at process start**: they tell you what *this running process*
loaded, not what the checkout says right now.

**One source answers every git-identity field.** A field it cannot
supply renders `unknown` rather than being filled in from elsewhere.
`git_dirty`, `git_describe` and `git_last_tag` read `unknown` wherever
real git cannot run (`watched-checkout`, `vcs-install`, sometimes
`image+mount`); that does not make the sha or branch doubtful.

## Is my MCP stale? `source_drift`, the WARN lines, and a missing row

`source_drift` is the one field read *live* rather than frozen, and it is
what answers "is my MCP stale?": `none` (the served tree is still at the
sha this process imported), `moved <old>→<new>` (it advanced; this process
is serving code the tree no longer has), or `unknown` (no
tree to compare, so **nothing was checked** — not a claim of freshness).
The shared session server serves the deployed prod clone, so a landed
change is not live until deploy.

Anything short of `none` is also said out loud as a `⚠ WARN` line under
the Build table, and any WARN makes the verdict `Overall: WARN` instead
of `OK`: `WARN staleness unknown — <what could not be compared>`,
`WARN served tree is N commits behind the mounted checkout (<old>→<new>)`
(N from git when it can read the tree; "behind" without a count when it
cannot), and `WARN build is <age> old` when `build_time` is older than
three days. `build_age` renders the age beside the timestamp so nobody
has to date-subtract by hand.

**A row missing entirely is the oldest signal of all**: a process that
started before `source_drift` (or `served_sha`) existed renders no such
row and no WARN. Absence means the process is older than the field, so
it is stale by definition and cannot tell you by how much. Do not read a
missing row as `none`. Reconnect (`/mcp` → the server → reconnect) to
land on the shared server. If the warning persists, report it to Reto,
the deployment owner, with `served_sha`, `source_drift`, `build_age` and
the WARN lines. Refreshing the server belongs to the deployment owner;
it is not the calling agent's job. Reconnecting does not deploy code.

## What version am I running?
## What version of precis-mcp is this?
## How do I check my precis-mcp version?
## How do I get the current precis version?
## What release am I on?
## What is my release tag?
## What's the precis-mcp release?
## How do I tell which precis build this is?

Call `get(kind='skill', id='precis-status')` and read the **Build**
section. It surfaces both `version` (from the installed package metadata)
and `git_last_tag` (the latest git tag
reachable from HEAD, e.g. `v8.4.4`). When no git facts are available
the git-derived fields render as `unknown`; the `version` field always
populates.

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
`git_source` (same value as `served_from`) names the lane;
`unknown` means no git, no baked build values and no install metadata, and
every git field renders `unknown`. With `image-build` nothing can be
compared, so the page carries `WARN staleness unknown`; `build_age` is the
signal.

## What database am I connected to?
## Which DB is this pointing at?
## What DSN is this using?
## What postgres server is this on?
## How do I see the connected database?
## What's the database host?
## How do I check what DB precis is using?

Call `get(kind='skill', id='precis-status')` and read the
**Database** section. Fields: `dsn_host` and `dsn_port` (the password is
never echoed back), `name`, `user`, and
`server_version`. When the DB is unreachable,
the section renders `unreachable: <ExcType>: <msg>` inline rather
than crashing the whole status call — this surface is the first
thing you hit *because* something is wrong, so it stays usable when
the DB is the thing wrong.
A pool timeout is split by the pool's own stats: `unreachable: cannot
connect to the database` (waiting won't help) versus `pool saturated`
(all connections busy; wait and retry).

## What migration version is the DB at?
## What schema version is this?
## What's the latest applied migration?
## How do I check the migration version?
## How do I see which migrations have run?
## How do I tell what schema version is deployed?

Same call. The **Database** section reports `migration` (the
highest applied migration, e.g. `0005_gripe_first_class_and_jobs`) and
`migration_count` (the total number applied).

`schema_state` compares the applied migrations to the ones *this build
packages*: `current`, `⚠ N migration(s) pending (first:
<plugin>/<version>)`, or `N migration(s) ahead of this package` (a
rolling-deploy peer migrated ahead). A pending `schema_state` means the
server booted onto a DB a migration behind — expect missing columns/tables
for whatever the pending migration adds until the migration lands.

## How do I look up a table's columns?

Read the live catalog through the `schema` kind (read-only, no rows):

```python
get(kind="schema")  # list tables
get(kind="schema", id="refs")  # columns, types, constraints, FKs both ways
get(kind="schema", id="refs", view="profile")  # sizes, distinct counts, index use
get(kind="schema", id="refs", view="jsonb", args={"column": "meta"})  # jsonb keys
```

Follow the next-call hints to hop along a foreign key.

Fallback when `schema` is unavailable: the generated reference through
`md` ([[precis-md-help]]) — `get(kind="md")` lists root aliases, then
`get(kind="md", id="<alias>/docs/reference/schema.md~refs")`. It is static
with its source date in the file, not live; compare `schema_state` above
when diagnosing drift.

## What container am I running in?
## What's the container hostname?
## How long has this process been up?
## What's the process pid?
## What python version is this build using?
## How do I check the runtime info?

Same call. The **Runtime** section reports `hostname`
(the container's name, not the host's),
`platform`, `python` (major.minor.patch), `pid`, `cwd` (the process
working directory), `started_at` (process start) and `uptime_seconds`.
Useful when a restart loop is suspected and you want to confirm "yes,
this process is fresh".

## Is my build out of date?
## Am I running the current code or an old one?
## Am I N commits behind — do I need to restart to refresh?
## How do I check for stale builds?
## How do I know if I need to rebuild?

Read the `⚠ WARN` lines under **Build** and `Overall:` at the bottom.
`Overall: OK` means every staleness check ran and passed; anything
the page could not verify is a WARN, never silence. Then `source_drift`:

- `none` — the served tree is still at the sha this process imported.
- `moved <old>→<new>` — the tree advanced and the process never
  restarted. It is serving the old code; the WARN says how many commits
  behind.
- `unknown` — no tree to compare, so nothing was checked; the WARN names
  why. Go by `build_age` and `started_at`, and reconnect if in doubt.
- *no `source_drift` row at all* — the process predates the field, so it
  is stale by definition. Reconnect rather than measuring; a process that
  cannot report drift also cannot tell you how far it has drifted.

`served_sha` is **frozen at the moment the process started** — it is what
*this running process* loaded, not what the checkout on disk says now,
and not the image's `image_sha`. Reading a source file from inside a
container does not tell you what the process is serving: the files can be
current while the process keeps the modules it imported hours ago.
`source_drift` compares a sha frozen at import against the tree now, so
it cannot agree by construction.

Other fields to cross-reference:

- `served_from` — `working-tree`/`image+mount` mean a live checkout;
  `image-build` means a baked image.
- `version` vs `git_last_tag` — a gap means the checkout is between
  releases.
- `git_dirty` — uncommitted changes were present when the process
  loaded. Fine for dev iteration; surprising in prod.
- `build_age` (image builds) — how stale is this image, as an age;
  past 3 days it is already a WARN.

## Is the running server the code in this directory?
## Are my edits live in the running server?

The status page answers **build staleness** — which code a process runs.
It is not a liveness or health check: for "is X healthy / up?" read
`get(kind='alert', id='/health')` first ([[precis-alert-help]] §Health
questions). Read `served_sha`, `git_branch`, `served_from` and
`source_drift` as above. A connected server on branch `main` is the main
tree, not a worktree: worktree commits never reach it until deployed.

## Is a dark service lit? (conflict_search, inbound_chase, ...)

Read the **Dark switches** table in `get(kind='skill', id='precis-status')`.
One row per dark-switched service: `state` is `lit` (a row
with priority >= 1), `off` (priority 0) or `dark` (no row: nobody has set a
priority, so it is not running; not a fault). `setting` lists the
controlling `host=priority` rows.

For `inbound_chase`, the table reports placement only. The worker rotation
requires positive resolved host priority and the live `inbound_chase.enabled`
flag. Read the flag with `get(kind='job', id='/settings')`.

## The server refused my call as busy — `the server is busy` / `retry_after_s`

A call that waits for a dispatch slot past `queue_wait_s` is refused
**before it ran**: `the server is busy: this <verb> waited Ns for a free
worker slot and was NOT run — nothing was written`. `NOT run` is the
guarantee — no write landed, no partial work happened, so retrying is
safe. The refusal carries `retry_after_s=<int>`: wait that many seconds,
then retry the same call. Retrying sooner just re-queues behind the same
held slots; several agents refused at once each wait their own
`retry_after_s` beat rather than stampeding back together.

The **Runtime** row `dispatch_slots` shows whether the ceiling is still
bound: `limit` is the slot count, `in_flight`/`queued` the live load, and
`refused_1h` how many calls were turned away in the last hour. A `refused_1h`
that keeps climbing on a server that otherwise answers is worth a gripe.

## See also

- [[precis-alert-help]] — health questions: `/health`, failure ids, why
  process presence is not health.
- [[precis-overview]] — orientation: seven verbs, one address scheme.
- [[precis-help]] — the synthesised skill listing active kinds + verbs
  on this server (from the live hub, not a file — still a valid
  wikilink target, resolved against the synth slug set).
