---
status: draft
title: One outbound policy for rates and timeouts, overridable per service without a deploy
pillar: operations
prio: high
---

# One outbound policy for rates and timeouts

Reto, 2026-10-23: "So we want to check rate limiter and have it used
throughout (timeouts etc we want, but we have a (code or db) override for
rates, timeouts. Defaults are fine and its great but some are special. I
suppose per service, with a dry thing?" Earlier (2026-10-21, gr503084
comment 1): a release-time checker that every async, subprocess and network
call has a timeout.

**Scope.** Owns the one policy resolver (`EffectivePolicy`) every outbound
call goes through, the class defaults, the DB override layer, the
deadline-aware wait, refusal accounting and the AST enforcement gate. Uses
two shipped primitives and adds no third: the **rate bucket** (one
`external_rate_limits` row per provider, `utils/rate_limit.py`, migrations
0121/0222/0223/0225 — "how many per second/day") and the **slot hold**
(`resource_slots` + `resource_slot_holds`, `store/_resource_slots_ops.py`,
migrations 0073/0119/0123 — "how many at once"). Every outbound rule —
website politeness, provider API, paid LLM API concurrency, castor lanes,
a future castor2 or spark endpoint — is a configuration of those two,
resolved here. Does not divide a local endpoint's slots among callers or
set its slot count: that is `llm-lane-scheduler.md` (the `llm-local`
class). Does not pick which job a worker runs: `cluster-scheduling.md` and
`job-scheduling-fairness.md`.

Point fixes landed first (2026-10-23): 1eabf9afe (gr507343, gr507340:
OpenRouter on a wide `LLM_HOST_ROW=(32, 16.0)` host row, classify
requeues instead of failing), 258392232 (gr507345: idle `host:` rows
evicted at the cap, `limiter-host-cap` condition), 622ff9556 (gr507344:
CORE search paced through `require("core")`), 5165ecf80 (fetch legs
timed: gate, limiter, request, deadline). S1 replaces the first; S4
finishes the second (§6).

## Motivation / why

Today's outbound stack is three half-policies that do not compose:

- `utils/http.py::Policy` holds backoff, breaker, spacing and
  `limiter_wait_s`; the limiter row (`external_rate_limits`) holds rate
  and daily cap; timeouts are a per-call `timeout=` argument with no
  registry. A service's effective behaviour is spread over three places
  and no view shows it.
- `_resolve_provider` seeds every unnamed host as `host:<netloc>` at
  `DEFAULT_HOST_ROW=(1, 1.0)`. LLM APIs went through it when `_polite`
  (`utils/llm/openai_tools.py`) was routed through `call_with_backoff`, so
  openrouter.ai is paced like a scraped website: ~20k refusals/day
  (gr507343, gr507340), invisible in `llm_call_log` because they happen
  before the provider call.
- `rate_limit.acquire` waits up to `max_wait_s` with no knowledge of the
  caller's deadline, so a limiter wait eats the ref-pass budget
  (`PRECIS_REF_PASS_DEADLINE_S`, 300 s; gr506756's class).
- A bare `http_client(...).get` (`fetch_oa.py::_query_core_fulltext_urls`,
  gr507344, fixed in 622ff9556) passed
  `tests/test_outbound_polite_ratchet.py` because that ratchet checks name
  presence per enclosing function, not per call; the next one will too.
- Fail-open is silent beyond a rate-limited WARNING (gr503435).
- Paid-LLM concurrency has no primitive at all: nothing caps how many
  OpenRouter calls the fleet has in flight, so a 429 storm is the only
  signal.

## The two primitives (shipped; folded from `shared-rate-limiter.md`)

| primitive | answers | row | atomic point | fail mode |
|---|---|---|---|---|
| rate bucket | per second, per day | `external_rate_limits(provider)`: token bucket + daily counter, `parked` via negative tokens | `rate_limit._CONSUME_SQL` (`UPDATE … FOR UPDATE`) | fail-open: no row / no DSN / DB down → unpaced, warned once per hour |
| slot hold | at once | `resource_slots(host, resource)` `free` counter + `resource_slot_holds` TTL lease with holder identity | `_RESERVE_ONE` conditional decrement; `reclaim_expired_slot_holds` + the epoch reaper refund a dead holder | a crashed holder's unit returns within one TTL or one epoch sweep |

Wired today on the rate bucket: `s2`, `openalex` (+100k/day), `unpaywall`
(100k/day), `arxiv`, `crossref`, the JLC rows (0223), every `host:<netloc>`
seeded by `utils/http.py`. The wiring map per call site is the
`rate_limit.py` module docstring; do not re-list it here. On the slot
hold: `llm:<model>` rows reserved by `local_serving.acquire` (bounded
wait `PRECIS_LOCAL_SLOT_WAIT_S`, spill to every LAN-routable `served_by`
host), and hardware rows reserved at job claim
(`workers/executors/_common.py`).

Reto's rulings on the limiter (2026-10-16 ~10:2xZ, morning-queue entry 7),
kept here because the item that held them is deleted:

1. Crossref gets at most 1 request/s: "Be nice. We have time." The cap is
   1/s whatever Crossref's headers say (`0222_crossref_rate_1rps.sql`).
   Header reading, one polite GET (`/works?rows=0` with the project
   mailto) at 2026-10-16 10:25:37Z: `x-rate-limit-limit: 3`,
   `x-rate-limit-interval: 1s`, `x-concurrency-limit: 3`,
   `x-api-pool: polite-array`.
2. Gate the on-demand and ingest-time Crossref lookups (`doi_backfill`,
   `metadata_resolve`, `provenance`, `doi_registry`, `crossref_citation`,
   `si_discovery`) through the same `require()` chokepoint. Shipped.
3. Re-match `bib_parse` entries left NULL-match by a plain Crossref failure
   (rate-limit or transport error, not a true no-match), at the limiter's
   pace; idempotent and bounded per run. Shipped
   (`bib_parse.run_bib_rematch_sweep`, ≤50 per run).

## In scope

### 1. One policy, resolved once per call

Every outbound call resolves one `EffectivePolicy`:

| field | meaning | primitive |
|---|---|---|
| `rate` | `(capacity, refill_per_sec, daily_cap)` or `None` (no token lane) | rate bucket row |
| `concurrency` | a `resource_slots` resource to hold for the call's duration, or `None` | slot hold |
| `max_wait_s` | longest limiter or slot wait before a refusal | — |
| `connect_s`, `read_s`, `total_s` | httpx phases + the wall-clock bound (`gr486823` trickle case) | — |
| `retry` | attempts, base, cap; 429 and `Retry-After` honoured (park the row) | — |
| `on_refuse` | `wait` (block up to `max_wait_s`), `defer` (requeue/skip, record), `fail` (raise `RateLimited` with `next=`) | — |

Covers HTTP to scraped hosts, provider APIs, LLM APIs, subprocess/ssh
(`total_s` only), and DB statement timeouts where a caller sets one
(`statement_timeout`/`lock_timeout` stay config-level, not per call).

### 2. Defaults by class, in code

| class | rate | concurrency | notes |
|---|---|---|---|
| `scraped-host` | 1/s (today's `DEFAULT_HOST_ROW`) | — | any `host:<netloc>` with no named row |
| `provider-api` | the documented limit (0225 rows) | — | s2, crossref, openalex, … |
| `llm-api` | none | a shared hold on `resource_slots` row `(host='api', resource='llm-api:<provider>')`, capacity per provider | paced by 429 + `Retry-After` (`park`); openrouter.ai, OpenAI-compatible hosts; shared hold ruled by Reto 2026-10-23 |
| `llm-local` | none | the endpoint's `llm:<model>` slots, divided by class | castor SGLang and sparks; `llm-lane-scheduler.md` owns the division and the slot count |
| `internal` | today's per-service pacing until a caller opts in (S1 review b78994bed: a rate-less default would silently drop the 1/s pacing on services that call internal URLs today); the target is none | — | `is_internal_host`, embedder, cluster services |

A rate-less row still needs a park: add `parked_until timestamptz` so
`park()` works without a token bucket. The S1 decisions below fix the
mechanics.

### S1 decisions (2026-10-23, answering the coordinator's readiness check)

**D1. From `LLM_HOST_ROW` to the rate-less `llm-api` class.** No row is
deleted and no row is rewritten. The class is a code attribute: `Policy`
gains `cls`, `POLICIES["openai-tools"]` sets `cls="llm-api"`, and the
DB `class` column is only an override. For an `llm-api` call the
resolver returns `rate=None`, so `acquire` is not called and the token
columns of `host:openrouter.ai` are ignored (left in place, harmless).
`LLM_HOST_ROW` and `Policy.host_row`'s LLM use are deleted in S1; the
row is still seeded on first use (default `(1, 1.0)` tokens, unused) so
it can carry `parked_until` and the overrides.
`_PARK_SQL` becomes two writes in one statement: `parked_until =
GREATEST(COALESCE(parked_until, now()), now() + seconds)` on every row,
and the existing negative-token write only `WHERE refill_per_sec > 0`.
Before a call, a rate-less class reads `parked_until` from the cached
row (D3) and, if parked, waits `min(park left, max_wait_s, deadline)` or
refuses per `on_refuse`. Rated rows keep parking through their tokens
as today; they also get `parked_until`, so `/limits` shows one park
column for every class.

**D2. The `llm-api` hold lifecycle.** One hold per logical LLM call, in
`utils/llm/openai_tools.py` around the request method (blocking and
streamed), outside `_polite`/`call_with_backoff`: retries reuse the
hold, and a streamed body is read inside it. Acquire =
`reserve_resource_slots(conn, 'api', {'llm-api:<provider>': 1})` +
`insert_slot_hold(..., holder_identity=worker_identity())`, the same
pair `local_serving.acquire` uses; release in `finally` =
`release_resource_slots` + `delete_slot_hold`. TTL = the client's hard
`timeout` + 60 s (not `PRECIS_SLOT_HOLD_TTL_S`'s 3600 s), so a crashed
holder's unit returns in minutes; the epoch reaper reclaims sooner when
the worker identity is replaced. A full row: poll as
`local_serving.acquire` does, up to `min(max_wait_s, deadline)`, then
`on_refuse` (default `defer` for workers: `RateLimited`, requeued by the
1eabf9afe classify path). A 429 parks the row (D1); a park longer than
the remaining wait refuses instead of sleeping on the hold.
`<provider>` is the netloc's provider name (`openrouter` for
openrouter.ai). The S1 migration seeds `('api', 'llm-api:openrouter')`
with capacity 16 (`ON CONFLICT DO NOTHING`); the operator edits
capacity on that row. A provider with no seeded row runs without a hold
and `_warn_unpaced` names it. DB down: per-process semaphore of
`PRECIS_OUTBOUND_FALLBACK_CONCURRENCY` (default 4).

**D3. Resolver.** New module `src/precis/utils/outbound_policy.py`:
`resolve(service: str, *, url: str | None = None) -> EffectivePolicy`.
Row key = `Policy.provider` if set, else `host:<netloc>` of `url`. Cache:
one `SELECT provider, <override columns> FROM external_rate_limits WHERE
<any override column> IS NOT NULL OR parked_until > now()` per process,
refreshed when older than 30 s (one query per process per 30 s, not per
call); a row absent from the snapshot has no overrides. Overrides are
invalidated by the TTL only; `precis limits set` does not signal. The
park is not read from the snapshot (a 30 s-stale park would let calls
through a 429, defeating D1): the per-call `last_used_at` touch is
`UPDATE … RETURNING parked_until`, the gate reads that fresh value, and
`park()` also writes through to the local snapshot (S1 review
b78994bed). Merge, per field,
later wins where non-NULL: class default (`CLASS_DEFAULTS[cls]`) →
`POLICIES[service]` field (new fields `max_wait_s`, `connect_s`,
`read_s`, `total_s`, `on_refuse`, `concurrency` default `None` =
inherit; existing backoff fields keep their values) → DB column → the
per-call `deadline`, which only shortens waits. Class: DB `class` column,
else `Policy.cls`, else `internal` for `is_internal_host`, `provider-api`
for a named `provider`, `scraped-host` otherwise. A resolver fault
returns the code layers alone and warns once per provider per hour.

**D4. One migration, owned by S1.** S1's migration adds every new
`external_rate_limits` column: `class`, `max_wait_s`, `connect_s`,
`read_s`, `total_s`, `on_refuse`, `concurrency`, `parked_until`,
`last_used_at`, `refused_today` (reset with `day_used` on `day_start`
rollover), `last_refused_at`, and seeds the `llm-api:openrouter` slot
row. S2b and S4 write columns S1 created; neither adds a migration.
`last_used_at` is set by `acquire` and by the rate-less pre-call check,
because a rate-less row never moves `last_refill` and the 258392232
eviction (keyed on `last_refill`) would otherwise retire a busy
`llm-api` row with its overrides. So S1, not S4, re-keys eviction on
`GREATEST(last_refill, last_used_at)` and never evicts a row carrying a
non-NULL override column.

**`llm-api` concurrency (Reto ruled shared, 2026-10-23; the reasoning, first recorded as
this spec's recommendation).** Use the shared slot hold, not
a per-process semaphore. A per-process cap multiplies by the worker count
(~5 hosts × passes), so the fleet overshoots a provider's concurrency
limit exactly when every host is busy, and a 429 storm is the only
feedback; the hold's accounting host is a label (as the castor row already
shows), so one row caps the fleet; the crash-leak path is already closed
by the TTL + epoch reaper; and it is one primitive, not a second
semaphore to observe. DB down degrades to a per-process cap
(`PRECIS_OUTBOUND_FALLBACK_CONCURRENCY`, default 4), the same shape
`llm-lane-scheduler.md` uses for lanes. Cost: one `UPDATE` per LLM call
against calls that last seconds.

### 3. Layered overrides, no deploy for a DB edit

Resolution order: code default → class default → DB row → per-call
`deadline`. `external_rate_limits` gains nullable columns `class`,
`max_wait_s`, `connect_s`, `read_s`, `total_s`, `on_refuse`,
`concurrency` (the resource name, or NULL), `parked_until`,
`last_used_at`; a non-NULL column overrides the layer below. The
`concurrency` resource's capacity lives on its `resource_slots` row, so
"how many at once" is edited where the holds are counted, never in two
places. The resolver caches rows per process for ~30 s, so an operator
edit takes effect within a minute and no call pays a query. `Policy`
keeps its backoff fields and gains the timeout and `on_refuse` fields;
`POLICIES` becomes the code-default layer.

Affordance: `get(kind='job', id='/limits')` already renders the rows
(`handlers/job.py::_render_limits_view`); extend it to show the effective
policy per provider/host with the layer each field came from, and the
hold row's `free/capacity` for a concurrency class. Edits:
`precis limits set <provider> --max-wait 10 --on-refuse defer
--concurrency 8 …` (`src/precis/cli/service.py` sibling), with
`--dry-run` printing the effective policy before and after.

### 4. Deadline-aware waits and counted refusals

`acquire(provider, *, deadline=None)` takes the caller's remaining budget
and waits `min(max_wait_s, remaining)`; the ref-pass loop
(`workers/runner.py::run_loop`, `ref_pass_deadline_s`) passes its
remaining budget down. The same `deadline` bounds a slot wait. A refusal
is counted in the row: `refused_today` and `last_refused_at` (columns
from S1's migration, D4; one counter for every class, no `llm_call_log`
rows), so demand is visible in `/limits` and the doctor's
`limiter-bypassed` probe
(`workers/conditions.py::_probe_limiter_bypassed`) gets a sibling
`limiter-refusing` finding.

### 5. Enforcement gate (merges the timeout checker from gr503084 B + C)

One AST ratchet test, shaped like `tests/test_text_io_encoding.py` and
`test_outbound_polite_ratchet.py`, run by the pre-qland lint, every gate
and CI:

- HTTP: `httpx`/`requests`/`urlopen`/`aiohttp`/`http_client(...).get|post`
  outside the policy layer, checked per call (the resolved callee goes
  through `with_backoff`/`call_with_backoff`), not per enclosing function.
- `subprocess.run/check_output/check_call/call`, `Popen.wait/communicate`
  without `timeout=`.
- `asyncio` network awaits without `wait_for`/`timeout`;
  `asyncio.Queue.get`, `Event.wait`; `Thread.join`, `Queue.get`,
  `Lock.acquire`, `Future.result`, `as_completed` without a timeout;
  `psycopg.connect` without `connect_timeout`; blocking `fcntl.flock`.
- `while True` polling loops with no deadline: report only.
- ruff `S113` and the `ASYNC` family on.

Baseline counts frozen in the allowlist (AST scan 2026-10-21: src
subprocess 25/77 bare, scripts 16/20, tests 222/302; HTTP in src all
carry timeouts); a new offender fails, the list only shrinks. The CORE
bypass is the motivating case. gr503084 keeps its part A (CI hang
debuggability: per-worker START markers, py-spy dump, leak detector);
parts B and C live here and the gripe says so.

### 6. Seeding cap

Shipped in 258392232: at `MAX_SEEDED_HOST_ROWS` (500) the seeder evicts
`host:` rows idle past `HOST_ROW_IDLE_EVICT_S` (24 h, keyed on
`last_refill`) and the `limiter-host-cap` condition fires when none is
idle. S1 re-keys the idle test (D4); left for S4: show occupancy in
`/limits`. The cap stays 500 unless the
condition fires in practice.

## Explicitly NOT in scope

- Dividing a local endpoint's slots among classes, floors, fillers, and
  the slot count per endpoint: `llm-lane-scheduler.md`.
- Which job a worker claims and where it runs: `cluster-scheduling.md`,
  `job-scheduling-fairness.md`.
- CI hang debuggability (gr503084 part A) and per-test timeouts (p0-ci).
- Changing any provider's published rate; 0225's rows and the Crossref
  ruling stand.

## Slices

| slice | delivers | absorbs |
|---|---|---|
| S1 | `EffectivePolicy` + resolver (D3), class defaults, the one migration (D4), `parked_until` park (D1), `llm-api` class with its shared hold (D2); deletes `LLM_HOST_ROW` | replaces 1eabf9afe's wide row |
| S2a | `deadline=` on `acquire`/`require` and on the local slot wait, ref-pass budget passed down by ContextVar, refusals logged | gr506756's limiter half |
| S2b | `refused_today`/`last_refused_at` written on refusal, `limiter-refusing` condition | — |
| S3 | the AST ratchet + ruff rules, allowlist frozen, `test_outbound_polite_ratchet` tightened per call | gr507344's class, gr503084 B+C |
| S4 | `/limits` effective-policy view + occupancy, `precis limits set/--dry-run` | gr503435's visibility half |

S2a and S3 ship alone, no migration. S2b and S4 need S1's migration
(D4) and are otherwise independent of each other.

## Acceptance criteria

- S1: `resolve('openai-tools', url='https://openrouter.ai/api/v1')` returns class `llm-api`, no token lane, a
  `concurrency` resource whose `resource_slots` row exists; two processes
  on two hosts each holding the row's capacity see the next call refused
  or waited, never a (capacity+1)th in flight; an `UPDATE
  external_rate_limits SET max_wait_s=5 WHERE provider='crossref'` changes
  the resolved policy within 60 s with no process restart; `prod` shows 0
  `RateLimited('host:openrouter.ai')` lines in `worker_logs` over 24 h
  while classify throughput holds; a 429 park is honoured by the next
  call on another process; a rate-less row used within the idle window,
  or one carrying an override, is not evicted.
- S2: a caller with 2 s left waits at most 2 s; the fetch pass on
  melchior logs its per-cycle summary row again (gr506756's signal) and
  the `/limits` view shows a non-zero refusal count for a starved row.
- S3: the ratchet fails on a synthetic bare `http_client().get` and a
  `subprocess.run` without `timeout=`; the frozen baseline passes; CI's
  lint lane runs it.
- S4: `get(kind='job', id='/limits?provider=core')` names the layer of
  every field.

## Target + blast radius

`utils/outbound_policy.py` (new), `utils/rate_limit.py`, `utils/http.py` (`Policy`, `POLICIES`,
`_resolve_provider`, `_take_token`), `utils/llm/openai_tools.py::_polite`,
`store/_resource_slots_ops.py` (hold for the `llm-api` class),
`workers/runner.py` (ref-pass deadline), `workers/conditions.py`,
`handlers/job.py::_render_limits_view`, `cli/service.py`, one migration,
`tests/test_outbound_polite_ratchet.py` + the new ratchet. Every worker
pass that fetches, and every LLM call, resolves a policy — a resolver
bug is fleet-wide, so the resolver fails open to today's code defaults
and logs once per provider per hour (`_warn_unpaced`).

## Open questions / decisions log

- Decided 2026-10-23: refusal accounting is a counter column for every
  class (D4); no `llm_call_log` refused rows.
- Decided, Reto 2026-10-23: `concurrency` for `llm-api` is the shared
  `resource_slots` hold (§2, D2), not per process.
- Whether `digikey`/`mouser`/`farnell` daily caps move to the class
  layer (`vendor-api`) or stay per row.
- Carried from `shared-rate-limiter.md` (deleted 2026-10-23): a
  `bib_parse` entry Crossref can never answer (a 400 on odd citation
  text, an unparseable body) stays `match_conf IS NULL` and the rematch
  sweep may re-pick it on every idle cycle — add a per-entry attempt
  marker if that shows up in the limiter budget.
