---
status: draft
title: SMALL-LLM derived-drain band — mint the derived SMALL queues as low-prio jobs, capped 6-on-melchior, never cloud
model: opus
---

# SMALL-LLM derived-drain band

## Motivation / why

Reto's ask, in his words: *"make a factory for derived-queue small work that
mints todos, that do the thing, and get worked off at a lower priority than
'normal work'. Just keep 20–50 around at any time, and if it falls below 20,
mint some more."* And the placement constraint that preceded it: the SMALL-tier
consumers should *"only [be] running on melchior … up to 6 at a time … They
should not go elsewhere"* — **local-only, capped, never cloud**.

Today there are **two** execution substrates and this work sits on the awkward
one:

- **Derived-queue passes** (`llm_summarize`, `classify`) — standing worker
  passes that scan a `WHERE needs-work` predicate, lease via `chunk_claims`,
  self-parallelize with an in-pass thread pool, and self-heal. Invisible to the
  job substrate: no `prio`, no `/factory` console row, no dispatch accounting.
- **Todo-tree jobs** (everything on an executor) — explicit `prio`, a
  dispatcher, visible on the console, drained by `job_inproc`/`claude_inproc`.

The embed queue already crossed over (§F cycle a/b): `embed_batch` is a
*bounded work-order job* that drains the derived embed queue, minted by the
`materialize` cadence. **This spec does the same for the two SMALL-LLM derived
queues** — summarize and classify — so all derived SMALL work flows through the
job substrate: prioritizable, console-visible, dispatch-accounted, and (the
placement half) slot-capped on melchior with no cloud path.

**The "only melchior, never cloud" redirect dissolves the hard part.** The
predecessor spec (`local-first-capacity-valve.md`, retired in this ship — git
history) tried to make SMALL run local *and spill the same model to cloud on
saturation* — which needed cross-host slot borrow, a LAN-exposed llama-swap
endpoint, and a same-model hosted slug. Reto's redirect is the opposite:
**never spill.** So none of that borrow/LAN/same-model machinery is needed. The
qwen slot already lives on melchior at cap 6; jobs pin there and reserve nothing
cloud-ward; saturation is pure backpressure (the 7th call waits, the queue holds
it), not overflow. This spec **supersedes** that valve proposal, retired here.

## Present-state facts this builds on (verified in code)

- **The router already caps concurrency at 6, locally, with backpressure.**
  `llm.chain.small`'s rung-0 is `{placement:local, model:qwen3.5-9b-q4_k_m,
  transport:local}`. On a saturated local slot, `Transport.LOCAL` returns
  `DispatchError.paused` and **does not escape** (router.py `dispatch`, the
  `slot.paused` block only escapes for `openai_compat`/`openai_tools` rungs).
  `run_llm_summarize_pass` already treats `paused` as a transient
  record-for-retry (llm_summarize.py:1204,1288). So "6 at a time, wait beyond,
  never spill" is *already the router's behaviour* — provided the SMALL chain
  has **no cloud rung** (today it still has a `glm-4.7-flash` rung-1; Slice 4
  drops it).
- **`materialize._BACKLOG_SOURCES` is the generalization seam.** Its
  `_BacklogSource(name, job_type, executor, count_fn, batch_limit, params_fn)`
  tuple was built generic on purpose ("a second backlog source is a new tuple
  entry, not new mint/hysteresis logic"). Hysteresis (mint only above HIGH +
  nothing while a batch is live), deterministic-tick idempotency keys, and
  `_in_failed_cooldown` are all already there.
- **`job_inproc` runs bounded jobs one-at-a-time, synchronously**, with
  `renew_own_lease` for long drains and slot-refund on any terminal status.
  `embed_batch` is the exemplar (workers/job_types/embed_batch.py).
- **`params.target_node` hard-pins a job to a host** (claim SQL filters
  `target_node IS NULL OR = %s`; `struct_relax` uses it for GPU pinning). This
  is the melchior pin — a hard gate, not the 10-min `LLM_AFFINITY_GRACE_MIN`
  soft affinity.
- **Both SMALL passes are already dark** (`enable_env`
  `PRECIS_SUMMARIZE_LLM`/`PRECIS_CLASSIFY_ENABLED`, **not** in
  `default_profiles`). So retiring them from any standing rotation is a no-op —
  there is nothing to turn *off*, only a minter to turn *on*.
- **Both passes share one shape:** `run_X_pass(store, *, client, batch_size,
  concurrency, …) -> {claimed, ok, failed}`, three short-txn phases (claim /
  LLM / write-back), no lock held across the LLM call. **Both claim `ORDER BY
  c.chunk_id`** — the head-of-line starvation the fair-dispatch work flags
  ([[fair-dispatch-two-currencies]] finding #1; the gr191337 lane-monopoly
  shape).

## Design

### The one execution unit: a `derived_drain` job

A single new job_type, `derived_drain`, generalizing `embed_batch` **one level
up**: instead of re-homing the summarize/classify logic into `WorkerHandler`
subclasses (they are *not* WorkerHandlers — they need DB JOINs, doc-card
prefetch, and an outbound LLM call, and they already own correct claim/retry/
write-back machinery), the job **wraps the existing `run_X_pass` function** and
drives it in a bounded loop.

```
params = {
  "pass":        "llm_summarize" | "classify",   # which derived queue
  "limit":       <int>,                          # chunks to drain this job (bound)
  "target_node": "melchior",                     # hard host pin
  "concurrency": 6,                              # in-pass thread-pool width == slot cap
  "batch_size":  16,
}
requires = frozenset()   # reserve NOTHING at the executor level (see "why no slot")
```

`_dispatch(ctx, spec)`:
1. Resolve `params.pass` → the `run_X_pass` callable + a matching
   backlog-count fn (a small dispatch table, extensible).
2. Build a **SMALL-tier, local-only** `DispatchClient` once (tier `SMALL`, which
   resolves through `llm.chain.small`; classify also builds its `escalate_client`
   from `PRECIS_CLASSIFY_ESCALATE_MODEL` exactly as `cli/worker.py` does today).
3. Drain loop, mirroring `embed_batch`:
   ```
   processed = 0
   while processed < limit:
       if not renew_own_lease(ctx.store, ctx.ref_id, ctx.meta): return   # lease lost
       res = run_X_pass(store, client=client, batch_size=min(batch, limit-processed),
                        concurrency=concurrency, ...)
       if res["claimed"] == 0: break        # derived queue empty
       processed += res["claimed"]
   ctx.append_chunk("job_summary", f"{pass}: drained {processed} chunk(s) …")
   ```
   The passes already write results back inline (per-chunk txns) and handle
   `paused`/empty as transient — so a saturated qwen slot just slows the drain,
   it never fails the job.
4. On an unexpected hard error, `ctx.record_failure(...)`; the materializer's
   `_in_failed_cooldown` (15 min) then prevents a mint-fail loop, same as embed.

**Why reserve NO executor slot (`requires=frozenset()`).** The 6-cap lives at
the *router* (`local_serving.acquire`, per LLM call). Each `run_X_pass` drives
up to `concurrency` concurrent calls; the router admits 6 and `paused`-backs the
rest. If the *job* also reserved the `llm:qwen…` slot at claim time, it would
**double-count** against those same 6 (job holds 1, then its calls try to
acquire more) and deadlock/starve itself. So the job reserves nothing; the pin
to melchior is `target_node`; the concurrency cap is the router slot. `job_inproc`
already runs one drain job at a time per worker, so there is no job-level fan-out
to cap anyway.

**Where the two numbers live** (they are different layers, both honoured):
- **"6 at a time"** = the router's `llm:qwen3.5-9b-q4_k_m` slot (cap 6) ==
  `params.concurrency`. Inference concurrency.
- **"20–50 in the queue"** = the minter band (below). Queue depth of *jobs*, so
  melchior's `job_inproc` always has a next `derived_drain` to claim between
  300 s cadence ticks. One drain job chews `limit` chunks and lasts a while, so
  the band is generous headroom, not a hard requirement — but it matches the ask
  and makes queue depth visible on `/factory`.

### The minter: per-source bands on the `materialize` cadence

Two new `_BacklogSource` rows (summarize, classify), each with:
- `count_fn` — a new `unsummarized_chunk_count` / `unclassified_chunk_count`
  (mirroring `unembedded_chunk_count`; the NOT-EXISTS predicate matching each
  pass's claim query, so count and claim can never disagree).
- `params_fn(limit)` → `{"pass": …, "limit": limit, "target_node": MELCHIOR,
  "concurrency": 6, "batch_size": 16}`.
- Its own hysteresis band. **Per-source independent bands, not one shared
  depth-proportional budget** — this is load-bearing, and corrects an earlier
  wrong instinct: depth-proportional minting lets a summarize flood (100k
  backlog) crowd classify (500 backlog) out of a shared band, and then even a
  perfectly fair dispatcher has *no classify jobs to run*. Independent bands
  guarantee each non-empty queue keeps its own buffer; an empty queue mints
  nothing (no wasted zero-row jobs). "Kinda equal" thus **emerges** — the minter
  computes no ratios.

**Schedule = the existing 300 s `materialize` cadence** (fleet-singleton via its
lease claim), *not* a wall-clock cron and *not* a new pass. The cadence interval
barely matters: hysteresis governs volume, the deterministic-tick `idem_key`
dedupes races, `_in_failed_cooldown` handles failure. Sizing note: keep the band
from fully draining inside one 300 s tick given melchior's throughput — lever is
`batch_limit`/band size; default band **HIGH=50, LOW=20** (mint back to ~50 when
below 20), `PRECIS_SMALL_BAND_*`-overridable, and each is dark until enabled.

Reuse everything: `_live_jobs`, `_mint_jobs` (idem keys become
`materialize:derived_drain:{tick}:{i}` — **note:** idem is keyed by `job_type`,
and both sources mint the *same* `job_type` `derived_drain`; either key the
sources distinctly (`_mint_jobs` idem prefix includes `src.name`) or give each
source its own `job_type` alias. **Decision: include `src.name` in the idem key**
so summarize/classify mints don't collide within a tick), `_MINT_PRIO=8`
(background, lower-than-normal — exactly the "worked off at lower priority"
ask), `_in_failed_cooldown`.

### Fair claim ordering (the "drain equally" half)

**SHIPPED (classify only).** Both passes claimed `ORDER BY c.chunk_id`, which —
because a paper's chunks are contiguous in `chunk_id` (ingested together) — lets
one big paper monopolize the lane and starves newest papers behind the whole
backfill. **Naive `ORDER BY random()` is a perf footgun** (seqscan + full sort of
the ~1M-row candidate set every claim, throwing away the PK-index walk). The
shipped fix, **classify's `_claim` only**:
- A **random `chunk_id` anchor** per corpus-sweep claim (`_random_chunk_anchor`
  — one cheap scalar), then `ORDER BY chunk_id >= anchor` with a **head top-up**
  (`< anchor`) if the forward slice runs short. Consecutive claims start at
  different papers → uniform coverage, and each slice stays a forward PK-index
  walk that stops at LIMIT (no sort). The first slice's in-statement lease INSERT
  is visible to the top-up (same txn), so no chunk is double-claimed.
- **Targeted (`ref_ids`) claims keep deterministic `chunk_id` order** — a scoped
  backfill/test wants reproducibility.

**Summarize is deliberately NOT reordered.** Its `ref_id, ord` contiguity is a
llama.cpp **prefix-cache** optimization (`llm_summarize.py:661-663`) — draining a
paper's chunks contiguously keeps the shared doc-header prompt prefix hot;
randomizing it would cause KV-cache misses and *slow* the drain. Summarize also
already has anti-starvation **tiers** (draft/conv/hot jump the backlog), which
classify lacks — which is why the anchor fix targets classify.

### Local-only, never cloud (the placement half)

Drop the cloud rung from the SMALL chain:
`llm.chain.small = [{"placement":"local","model":"qwen3.5-9b-q4_k_m","transport":"local"}]`
(remove the `glm-4.7-flash` rung-1). With no `openai_compat`/`openai_tools`
rung, the router's saturation-escape has nowhere to go → `paused` → the pass
records-for-retry → the chunk drains on a later batch when a slot frees. **Zero
cloud spend on SMALL by construction.** (Ops step — Slice 4.)

## Slices (ship order)

- **Slice 1 — `derived_drain` job_type + registry + tests. DARK.** The core
  mechanism, low blast radius. Provable by a manual `put(kind='job',
  job_type='derived_drain', executor='job_inproc', params={pass:'classify',
  limit:32, target_node:'melchior', concurrency:6})`. Nothing mints it yet.
  Ships like `embed_batch` did: deployed-dark, exercised by hand first.
- **Slice 2 — minter bands.** Add the two `_BacklogSource` rows + the two
  count fns; per-source enable flags (`PRECIS_SMALL_BAND_SUMMARIZE`,
  `PRECIS_SMALL_BAND_CLASSIFY`), **default-OFF**. Include `src.name` in the
  idem key.
- **Slice 3 — fair claim ordering. SHIPPED.** classify's `_claim` uses a random
  `chunk_id` anchor + head top-up (index-friendly, no sort); summarize left on
  its prefix-cache-friendly `ref_id, ord` order (see above).
- **Slice 4 — activation (ops, staged; runbook below).** All CODE is shipped;
  activation is private-overlay config + one `app_settings` flip + a supervised
  live cutover. See the runbook.

## Slice 4 activation runbook (ops)

**Verified prereqs (2026-08-11, read-only prod):**
- ✅ `resource_slots` `llm:qwen3.5-9b-q4_k_m` on **melchior**, capacity **6**,
  free 6 — the cap-6 semaphore is live.
- ❌ **BLOCKER: `PRECIS_NODE` is unset on melchior.** The `job_inproc` claim gate
  compares `meta.params.target_node` against `os.environ["PRECIS_NODE"]` (→ NULL
  when unset), so a job pinned `target_node="melchior"` matches **no host** and
  strands. Must set `PRECIS_NODE=melchior` on melchior's `precis_worker_agent`
  before the bands can drain.
- ⚠️ `job_inproc` is registered on melchior (`--profile agent` … actually
  `--profile all` union) but has been idle ~7 days (nothing pinned to claim) —
  expected; it will claim once `derived_drain` jobs mint AND `PRECIS_NODE`
  matches.
- ⚠️ Current SMALL routing is **~4.5:1 cloud** (`glm-4.7-flash` 12.4k vs local
  qwen 2.8k over 2 days) — the standing `llm_summarize`/`classify` passes hit
  cloud heavily today. Forcing all-local shifts ~5× load onto melchior's 6
  slots; the backlog will grow (backpressure — *this is the intended
  "queue beyond 6, never cloud"*), so expect a rising `unsummarized`/
  `unclassified` count until the 9B catches up (or indefinitely if it can't —
  a deliberate throughput ceiling).

**Config lives in the private overlay** (`inventory/group_vars/all/precis_env.yml`
via `precis_shared_env`, host_vars) — not in-repo. Set:
1. **`PRECIS_NODE: melchior`** — host_var for melchior's `precis_worker_agent`
   (the blocker fix).
2. **`PRECIS_SMALL_BAND_SUMMARIZE: "1"`**, **`PRECIS_SMALL_BAND_CLASSIFY: "1"`** —
   fleet-wide `precis_shared_env` (the materializer is a fleet-singleton, any
   host may run the tick, so the flags must be everywhere). Optional tuning:
   `PRECIS_SMALL_BAND_LOW`/`_HIGH` (default 20/50), `PRECIS_SMALL_DRAIN_LIMIT`
   (500), `PRECIS_SMALL_DRAIN_CONCURRENCY` (6), `PRECIS_SMALL_DRAIN_TARGET_NODE`
   (default `melchior`).

**Staged sequence (do NOT reorder — the cutover step can stop SMALL work if the
bands aren't proven draining first):**
1. Deploy the shipped code (`scripts/deploy`).
2. Set overlay vars 1+2 → deploy. **Do NOT touch the SMALL chain or standing
   passes yet.** Now the bands mint `derived_drain` on melchior *alongside* the
   still-running standing passes (they cooperate via `chunk_claims` leases — no
   conflict, some cloud spend continues).
3. **Verify the bands actually drain** (the gate): `derived_drain` jobs appear
   `queued`/`running` on melchior only; `llm_call_log` shows fresh
   `qwen3.5-9b-q4_k_m` local rows sourced `llm_summarize`/`classify`; ROLE3 tags
   + summaries land; the stuck-queue WARNING ("band full … 0 running … nothing
   is draining") stays **silent**. If it fires → the `PRECIS_NODE` pin still
   doesn't match or melchior's worker is down; fix before step 4.
4. **Only after step 3 is green — the cutover:** (a) disable the standing passes
   (`PRECIS_SUMMARIZE_LLM`/`PRECIS_CLASSIFY_ENABLED` off, or their
   `service_config` rows) so they don't double-work or break; (b) drop the SMALL
   cloud rung in prod `app_settings`:
   `UPDATE app_settings SET value =
   '[{"placement":"local","model":"qwen3.5-9b-q4_k_m","transport":"local"}]'
   WHERE key='llm.chain.small';` (via `scripts/prod-psql`; today it also carries
   a `glm-4.7-flash` cloud rung — that is the spill this removes). Now SMALL is
   melchior-local-only, capped 6, queues beyond, **zero cloud**.
5. **Watch:** zero SMALL cloud rows in `llm_call_log`; `unsummarized`/
   `unclassified` backlog rises then plateaus/drains per the 9B's real
   throughput; the reader still gets summaries (if the backlog starves the
   reader unacceptably, raise the band or re-add a cloud rung — the knobs are
   all live).

## Explicitly NOT in scope

- **The prio/fairness *scheduler* half** ([[fair-dispatch-two-currencies]]) — the
  work-conserving, good-mix, user-first candidate-picker. This spec makes SMALL
  work *into jobs with a low prio*; how the dispatcher fairly interleaves them
  against normal work (the "two currencies: cloud $ vs local slots" model) is
  that companion spec. Slice 3 here is only the *intra-pass* claim order, not the
  cross-root dispatch scheduler.
- **Cloud spill / same-model overflow** — the superseded valve. Deliberately
  removed, not deferred.
- **MEDIUM/BIG tiers** — this is SMALL-only (the high-volume classifier/
  summarizer lane where local-primary pays off).
- **Content-sensitivity placement** — orthogonal; SMALL is local anyway.

## Acceptance criteria

- A manually-put `derived_drain` job on melchior drains N chunks of its named
  pass (summaries / ROLE3 tags appear), renews its lease across the drain, and
  finalizes SUCCEEDED with a `job_summary` count. A non-melchior host never
  claims it (target_node).
- With Slice 2 on: below-LOW backlog on a non-empty SMALL queue mints
  `derived_drain` jobs up to HIGH at prio 8; an empty queue mints none; a
  summarize flood does not starve classify's band.
- With Slice 4 on: steady state = **zero** SMALL cloud rows in `llm_call_log`;
  under a burst, work queues (jobs pile to HIGH, `paused` backs off) rather than
  spilling; melchior's 6 slots are the only inference path.
- Fair ordering: a corpus with one 10k-chunk paper does not drain that paper to
  completion before touching others (spot-check claim order).

## Target + blast radius

- **New:** `src/precis/workers/job_types/derived_drain.py`; registry
  `ServiceSpec(kind=JOB, name='derived_drain')`; `unsummarized_chunk_count` /
  `unclassified_chunk_count` (in `llm_summarize.py` / `classify.py`).
- **Edit:** `workers/materialize.py` (two `_BacklogSource` rows, per-source
  band env, `src.name` in idem key); `llm_summarize.py` + `classify.py` claim
  `ORDER BY`.
- **Ops (Slice 4):** prod `app_settings.llm.chain.small`; two band flags;
  verify melchior `resource_slots` row.
- **Delete on ship:** `docs/backlog/local-first-capacity-valve.md` (superseded).

## Open questions

- **Band defaults (50/20) vs melchior throughput** — if one `derived_drain`
  (limit=?) drains in ≪300 s, the band can dip below LOW between ticks. Tune
  `limit` so a job lasts ≳ one tick, or shorten the `materialize` cadence for
  this source. Start 50/20 + `limit=500`; measure.
- **`concurrency=6` vs the router slot** — confirm melchior's
  `llm:qwen3.5-9b-q4_k_m` `max_parallel` is exactly 6 so pass-concurrency and
  slot-cap agree (a pass asking for more than the slot admits just eats `paused`
  backoff — harmless but wasteful thread-pool width).
- **One `job_type` for both passes vs two** — a single `derived_drain` with a
  `params.pass` discriminator (chosen) keeps the registry/console to one row;
  the cost is the idem-key `src.name` caveat above. Two aliases
  (`summarize_drain`/`classify_drain`) would be more console-legible but
  duplicate the spec row. Revisit if the console wants per-pass rows.
