---
status: draft
pillar: local-compute
title: Own aggregate embedder capacity across the sibling-container fleet
prio: high
---

# Own aggregate embedder capacity

Evidence: peer sessions rustling-questing-wadler + nanobuds, 2026-09-30, and
three gripes verified against current code/state this session.

`gr457326` (open, most recent comment 2026-09-30 ~14:23Z): the shared
session MCP's md-index vector warmup (`_warm_md_index_background`,
`src/precis/server.py`) batches into `embed_missing(batch_size=)`
(landed, qland `3ce020ed`) with bounded retry — a real fix for the
all-or-nothing-batch failure mode. But the dogfood after landing found the
deeper cause unaddressed: **twelve `precis-mcp-*` containers (one
`precis-mcp-http` plus eleven `precis-mcp-dev-*`) boot-warm the *same*
20,137-block tree against *one* embedder** (`host.docker.internal:8181`,
`max_inflight=4`). In the observed window, not one batch landed in 1h36m of
retrying — 429s clearing in 0.6s repeatedly, contention, not an outage.

`gr450123` (open; Reto's ruling 2026-09-28 implemented (b) bounded wait
queue + (d) `retry_after_s` propagation, landed `943aba15`) diagnosed the
same root shape from the request-path side: `embedder_interactive_max_concurrency`
bounds one process's in-flight embeds, `max_inflight` is a single *global*
gate, and nothing coordinates N sibling containers against it. Host-level
admission (option (a) in that gripe) was **explicitly deferred** — "until
(b) proves insufficient." `gr457326`'s dogfood is evidence (b) alone does
not cover the boot-warm-storm case, since that traffic isn't the
interactive request path (b) targets.

`gr456034` (open): a separate, related capacity symptom — `embed_batch`
jobs mint correctly but go unclaimed for hours (`live_jobs=0` the dominant
state), a `job_inproc` executor-capacity question, not the embedder
service itself. Filed here as adjacent evidence that "embedding capacity"
has more than one bottleneck in the current architecture.

## ⚠ Premise correction (2026-09-30, gr458940)

This item's evidence chain says `gr457326`'s dogfood shows gr450123's
option (b) — the bounded wait queue — is insufficient, which is what
trips that gripe's deferral of option (a), host-level admission. **That
inference does not hold: (b) has never run.** The embedder service on
8181 has been up since 2026-08-31; `943aba15` landed (b) + (d) on
2026-09-28. Its `/metrics` emits neither `precis_embedder_queued_total`
nor the two `queue_wait_seconds` counters current code emits, and its
429s carry no `retry_after_s` body field. It still sheds instantly at
the inflight cap — the exact pre-(b) behaviour (b) removed. A direct
probe served 64 texts in 9-11 s three times running with no 429, so the
observed 429 storm is not capacity.

Restart the agent (`com.precis.embedder`), re-measure, and only then
decide whether (a) is warranted. The shared-cache question below is
unaffected — twelve containers computing identical vectors is wasteful
regardless of how the service sheds.

## Motivation / why

Nobody owns *aggregate* embedder capacity across the fleet. Each fix so far
(batching, wait queue, retry-after) improved one caller's behavior against
a shared, uncoordinated resource — the fleet-wide picture (N containers,
one embedder, no shared cache) is still nobody's job. Local LLM rungs
(`local-rungs-small-medium.md`) landing on the same box would add a second
contender for the same host capacity with the same absent ownership.

## In scope

- **Name an owner** — a component/role responsible for the embedder's
  aggregate capacity across all sibling containers, not per-process
  bulkheads that don't know about each other.
- **A capacity number** — what the embedder can actually sustain
  fleet-wide (`max_inflight` today is one process's view; the real ceiling
  under N containers is unmeasured).
- **A budget/bulkhead decision** — whether a host-level admission token
  (gr450123's deferred option (a)) is now warranted, given (b) alone
  didn't survive the twelve-container boot-warm case.
- **The shared-cache decision** (Reto's call, per the brief) — whether the
  twelve containers should share one vector cache instead of each
  independently computing the same 20,137 blocks' embeddings, and if so,
  the multi-writer correctness question on the `.npz` cache file
  (`/home/precis/.cache/precis/md-vectors/bge-m3-1024.npz` per `gr457326`'s
  comment 5) that sharing would raise.

## Explicitly NOT in scope

- Re-fixing what `gr450123`/`gr457326` already landed (batching, wait
  queue, retry-after) — this item is the fleet-wide layer above those
  per-call fixes.
- `gr456034`'s `job_inproc` claim-starvation problem — cited as adjacent
  evidence, not this item's scope; it needs its own fix on its own timeline.

## Acceptance criteria

- A documented owner for aggregate embedder capacity exists (component,
  not "the embedder team" — there is no team).
- A measured fleet-wide capacity number exists, sourced from an actual
  N-container load test, not the single-process `max_inflight` default.
- The shared-cache question is decided (share or don't, with the
  multi-writer correctness answer if shared) and, if "share," implemented;
  if "don't," the boot-warm-storm cost is accepted explicitly rather than
  left as a standing unmeasured tax.
- `gr457326`'s dogfood scenario (twelve containers, cold boot, same tree)
  re-run and shown to warm within a stated time bound.

## Target + blast radius

`src/precis/embedder_service.py`, `src/precis/embedder.py`,
`src/precis/md_index/vectors.py` (the warmup/cache machinery); the
deploy-time container topology (how many sibling containers exist and
whether that count itself should shrink is in scope for the owner to
weigh in on, per `td458385`'s move-to-shared-HTTP-server direction, which
already reduces N from twelve toward one for session MCPs specifically).

## Open questions / decisions log

- **Open, Reto's call**: shared vector cache across containers — yes/no,
  and if yes, the multi-writer mechanism.
- **Open, Reto's call**: whether a batch-timeout-with-bulkhead embedder
  budget (host-level admission token, gr450123 option (a)) should now be
  built, given (b) alone proved insufficient for the boot-warm case.
- Note: `td458385` (session MCPs moving to the shared HTTP server) shrinks
  the container count this item worries about, but does not eliminate the
  underlying fleet-vs-one-embedder shape — dev containers and the shared
  server both still boot-warm against the same embedder.

Closest existing items: `embed-freshness.md`, `session-mcp-http-server.md`,
`mcp-shared-server-multiprocess.md`, `gr457326`, `gr450123`, `gr456034`.

## The md-warmup evidence is spent — what remains is admission (2026-10-01)

This item cited gr457326's dogfood as proof that the bounded wait queue
was insufficient and host-level admission was warranted. That inference no
longer holds, and not for the reason gr458940 gave either. The whole md
warmup failure was the warm pass starving the service it was waiting on:
batches sized at 64 blocks collected the long ones (up to 22756 chars),
blew the 15 s client budget, and orphaned server-side computation that
kept its slot — so the pass manufactured the saturation that then rejected
its own retries. Four fixes closed it, in order: per-batch retry
(d9bd4e16) made progress possible, the cold-cache re-arm (2b729531) made
it recur, a 16-block / 25 000-char cap (5146528d) made batch 1 pass, and
skip-and-continue on a bad batch (ae7bdb6a) actually drained the queue.
Measured on the shared server: **1% → 84% of blocks indexed**, cache
1.05 MB → 45 MB in under half an hour, after thirteen hours of zero
progress. The embedder timed idle is ~0.17 s for one short string and
3.6 s for 64 blocks — an order of magnitude faster than it looked under
self-inflicted load. **The hardware is not the constraint; do not buy
capacity on this evidence.**

What survives, and it is the part this item should now be about: a warm
batch and a one-string interactive query compete for the same four
undifferentiated slots. While a batch runs there is no usable interactive
capacity — a query embed is either rejected in under a millisecond or
admitted into a multi-second wait. Right-sizing the batch shortens that
window but does not separate the classes, so a large enough corpus or a
second batch client reopens it. That is a server-side admission question:
either a reserved slot for single-text requests, or a priority queue that
lets an interactive embed overtake a background batch.

Urgency has genuinely dropped rather than moved — passes now complete, so
the window is narrow. Reclassify accordingly rather than treating the
September evidence as live.
