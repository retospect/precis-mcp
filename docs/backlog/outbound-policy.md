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

Point fixes already in flight land first; this item generalises them and
must not block them: g4-llm-route owns gr507340 and gr507343 (classify
refusals, OpenRouter on the 1/s host bucket), g4-workers owns gr507344 and
gr507345 (CORE bypass, host-row cap). S1 absorbs whatever shape they land.

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
  gr507344) passes `tests/test_outbound_polite_ratchet.py` because that
  ratchet checks name presence per enclosing function, not per call.
- `MAX_SEEDED_HOST_ROWS=500` is full on prod; every new host runs unpaced
  and nothing alerts (gr507345). Fail-open is silent beyond a rate-limited
  WARNING (gr503435).
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
| `llm-api` | none | a shared hold on `resource_slots` row `(host='api', resource='llm-api:<provider>')`, capacity per provider | paced by 429 + `Retry-After` (`park`); openrouter.ai, OpenAI-compatible hosts; see Q3 below |
| `llm-local` | none | the endpoint's `llm:<model>` slots, divided by class | castor SGLang and sparks; `llm-lane-scheduler.md` owns the division and the slot count |
| `internal` | none | — | `is_internal_host`, embedder, cluster services |

A rate-less row still needs a park: add `parked_until timestamptz` so
`park()` works without a token bucket.

**Recommendation on `llm-api` concurrency (Reto's pending Q3; recorded as
this spec's recommendation, not a ruling).** Use the shared slot hold, not
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
is counted: a `limiter_refusals` counter per provider in the row (or an
`llm_call_log` row with `status='refused'` for the LLM classes), so demand
is visible in `/limits` and the doctor's `limiter-bypassed` probe
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

`last_used_at` on every row; a sweep prunes `host:` rows idle >30 days;
seeding at the cap raises an alert (`workers/conditions.py` condition
`limiter-cap-full`) instead of failing open silently. The cap rises to
2000.

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
| S1 | `EffectivePolicy`, class defaults, DB columns + migration, resolver cache, `llm-api` class for OpenRouter/OpenAI-compatible hosts with its shared hold row | gr507343, gr507340 (after the g4-llm-route fix lands) |
| S2 | `deadline=` on `acquire`/`require` and on the slot wait, ref-pass budget passed down, refusal counter + condition | gr506756's limiter half |
| S3 | the AST ratchet + ruff rules, allowlist frozen, `test_outbound_polite_ratchet` tightened per call | gr507344's class, gr503084 B+C |
| S4 | `/limits` effective-policy view, `precis limits set/--dry-run`, seeding sweep + cap alert | gr507345, gr503435's visibility half |

Each slice ships alone; S2–S4 do not depend on each other.

## Acceptance criteria

- S1: `resolve('openrouter.ai')` returns class `llm-api`, no token lane, a
  `concurrency` resource whose `resource_slots` row exists; two processes
  on two hosts each holding the row's capacity see the next call refused
  or waited, never a (capacity+1)th in flight; an `UPDATE
  external_rate_limits SET max_wait_s=5 WHERE provider='crossref'` changes
  the resolved policy within 60 s with no process restart; `prod` shows 0
  `RateLimited('host:openrouter.ai')` lines in `worker_logs` over 24 h
  while classify throughput holds.
- S2: a caller with 2 s left waits at most 2 s; the fetch pass on
  melchior logs its per-cycle summary row again (gr506756's signal) and
  the `/limits` view shows a non-zero refusal count for a starved row.
- S3: the ratchet fails on a synthetic bare `http_client().get` and a
  `subprocess.run` without `timeout=`; the frozen baseline passes; CI's
  lint lane runs it.
- S4: `get(kind='job', id='/limits?provider=core')` names the layer of
  every field; the host-row count on prod drops below the cap after the
  sweep and `limiter-cap-full` fires in a test with a full table.

## Target + blast radius

`utils/rate_limit.py`, `utils/http.py` (`Policy`, `POLICIES`,
`_resolve_provider`, `_take_token`), `utils/llm/openai_tools.py::_polite`,
`store/_resource_slots_ops.py` (hold for the `llm-api` class),
`workers/runner.py` (ref-pass deadline), `workers/conditions.py`,
`handlers/job.py::_render_limits_view`, `cli/service.py`, one migration,
`tests/test_outbound_polite_ratchet.py` + the new ratchet. Every worker
pass that fetches, and every LLM call, resolves a policy — a resolver
bug is fleet-wide, so the resolver fails open to today's code defaults
and logs once per provider per hour (`_warn_unpaced`).

## Open questions / decisions log

- Refusal accounting: a counter column vs `llm_call_log` rows. Column is
  cheaper and covers non-LLM providers; the LLM class may want both.
- **Q3 for Reto** — `concurrency` for `llm-api`: shared through a
  `resource_slots` hold (this spec's recommendation, §2) or per process.
  Pending his ruling; S1 builds the shared hold unless he says otherwise.
- Whether `digikey`/`mouser`/`farnell` daily caps move to the class
  layer (`vendor-api`) or stay per row.
- Carried from `shared-rate-limiter.md` (deleted 2026-10-23): a
  `bib_parse` entry Crossref can never answer (a 400 on odd citation
  text, an unparseable body) stays `match_conf IS NULL` and the rematch
  sweep may re-pick it on every idle cycle — add a per-entry attempt
  marker if that shows up in the limiter budget.
