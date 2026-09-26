---
status: draft
prio: normal
---

# Embed freshness

Grouped 2026-09-26 from 2 items that are sub-parts of one deliverable (each keeps its own section below; the originals are in the history). Split a section back out only when it becomes independently shippable.

## Scope-aware three-state embed-status hint

_Grouped 2026-09-26; was `embed-status-hint-three-state`, status draft, prio normal._

From gripe 244419's fix plan (item C). Separable — depends on nothing and
blocks nothing.

### Motivation / why

After touching a chunk the caller may want to know when the embedding lands.
The chunk row is available immediately; only the derived rows lag. Today
there is no way to ask, so an agent either guesses or re-searches blindly.

No new bookkeeping is needed — the state already exists.
`workers/embed.py::unembedded_chunk_count` decides staleness by comparing
`chunk_embeddings.content_sha` against `chunks.content_sha` (the same
predicate `EmbedHandler`'s derived-queue claim uses). So the scope you just
edited *is* the receipt: generalize that helper to take a scope and let the
caller poll it. Self-correcting, nothing to leak or garbage-collect, still
correct across worker restarts and concurrent sibling edits.

Bonus: the same hint makes the existing ~1M-chunk backlog legible per-scope
for the first time, instead of one global number.

> **2026-09-19, partial:** `unembedded_chunk_count(conn, ref_id=…)` and
> `unsummarized_chunk_count(conn, ref_id=…)` gained the scope parameter
> (unscoped byte-identical; scoped counts a `status='failed'` row as NOT
> embedded, plus `failed_embedding_count`), surfaced as the paper toc header's
> `readiness:` line. Still open: the caller-pollable three-state hint on an
> arbitrary scope (draft slug / chunk-id set).

### In scope

- A scope parameter (ref_id / draft slug / chunk-id set) on the existing
  `unembedded_chunk_count` predicate, exposed to the caller.
- **Three** states, not two: `current` (sha matches) / `pending` (no row, or
  row with a different sha) / `failed` (row with `status = 'failed'`).

### Explicitly NOT in scope

- **A server-side `wait=True`.** Blocking the MCP call until embeddings land
  holds an anyio worker thread for the duration — that is exactly the
  gr244419 wedge reintroduced through a friendlier door. Waiting must be
  client-side polling on a cheap count.
- Changing `unembedded_chunk_count`'s corpus-wide contract. `materialize`'s
  backlog high-water threshold and `embed_batch`'s `queue_remaining` share it
  precisely so the two can never disagree about what "backlog" means (§F
  cycle a) — the scoped form must not perturb the unscoped one.

### Acceptance criteria

- The existing `unembedded_chunk_count(conn)` call sites
  (`workers/materialize.py`, `workers/job_types/embed_batch.py`) return
  byte-identical numbers.
- A chunk whose only `chunk_embeddings` row has `status = 'failed'` reports
  `failed`, NOT `current`. This is the load-bearing case: today's
  `NOT EXISTS` clause treats `status = 'failed'` as satisfied (i.e. "don't
  retry"), so a permanently-failed chunk reads as not-stale and a naive wait
  loop would return success on exactly the case you'd most want to know
  about.
- Polling the hint over a scope costs one indexed query, not a per-chunk fan-out.

### Target + blast radius

`workers/embed.py::unembedded_chunk_count` and its two existing call sites;
whichever verb surfaces the hint. Read-only — no write path changes.

### Open questions / decisions log

- Which verb carries it? A field on the `sub=`/edit response (alongside the
  `{touched: N, embed_stale: N}` fan-out report), a `get`-side status, or
  both. The edit-response field is the one the polling loop actually needs a
  starting number from.

## Fresh hubs are invisible to semantic dedup for up to an hour

_Grouped 2026-09-26; was `fresh-hubs-invisible-to-semantic-dedup`, status draft._

Found 2026-08-22, right after minting fi237847.

### The gap

Ref-level search gained a semantic leg in `b634155c`, which is what finally
made the standing rule — *search for a proximate hub before minting; strengthen,
don't duplicate* — enforceable. But the semantic leg reads
`chunk_embeddings`, and chunk embedding is **demand-driven**, not eager:
`embed:bge-m3` is manual-only (`cli/worker.py`), and prod drains the queue
through the demand materializer's `embed_batch` / `job_inproc` path.

Observed cadence of `embed_batch` on 2026-08-22: 09:53, 10:17, 11:20, 12:09,
13:01, 14:14 — roughly hourly, all `succeeded`. fi237847's `finding_body` chunk
was created 14:50 and was still unembedded at 16:47.

So a hub minted at T is not semantically findable until the next batch, up to
~1 h later (longer if a deploy bounce delays the materializer — see
`deploy-drain-wait-is-a-silent-noop.md`). During that window:

- `search(kind='finding')` finds it only if the **title-lexical** leg matches,
  which is the AND-over-title behaviour the hybrid work existed to stop relying
  on. A paraphrase query will not find it.
- Two agents minting near-duplicate claims inside the same window cannot see
  each other's hub semantically — exactly the duplicate-hub failure the rule
  exists to prevent, and the mechanism behind existing pairs like
  fi191259/fi191268.

Confirmed live: `"how does neck length affect the conductance plateau in a
nanobud"` — the claim fi237847 states almost verbatim — returned three other
hubs and not fi237847.

### Why it isn't just "wait an hour"

The dedup gate is the one place where staleness is not benign. Everywhere else
a missing embedding costs recall, and the answer arrives late. Here it costs
**correctness of the corpus**: the miss is silent, and its consequence (a
duplicate hub with its own `pub_id` and its own evidence edges) is expensive to
undo — merging hubs is a manual adjudication, which is why the remediation
backlog has a dedup pass at all.

> **2026-09-19:** the agent `put(kind='finding', supporters=)` door now runs
> the block→dedup_judge→place cascade (`handlers/_finding_hub_mint.py`),
> which makes this window bite harder: a root converging several readers'
> proposals in one session mints duplicates whenever two proposals paraphrase
> one claim, because the first mint is not embedded when the second arrives.
> The `precis-read-for-question` skill works around it by clustering
> proposals before the door. Option 1 is still the fix; the embedder is
> already in hand at that door (`embedder=`), so a hub-only synchronous embed
> of the new `finding_body` chunk right after `mint_hub` is one call.

### Options

1. **Embed hub chunks synchronously at mint.** A claim hub is one short
   sentence — a single embed call, not a batch. `mint_hub` /
   `refine_claim_sentence` already write inside one transaction; enqueueing a
   priority embed (or calling the embedder directly, degrading to the queue on
   failure) closes the window entirely for the kind that needs it most. Note
   the standing rule that ingest must not call `fill_embeddings` — this would
   need to be a deliberate, hub-only carve-out via the worker/job path, not a
   general loosening.
2. **Prioritize `finding_body` in the materializer** so hub chunks jump the
   queue, shrinking but not closing the window.
3. **Make the blind window explicit at the mint door.** Have `direct-mint` (and
   the `put(kind='finding')` hub path) report how many candidate chunks are
   unembedded, or refuse to claim "no matching candidate" when the hub's own
   cohort has pending embeddings — an honest "dedup ran degraded" beats a
   confident false negative.

(1) is the real fix; (3) is cheap and worth doing regardless, since it makes
the failure loud instead of silent.

### Rejected: bumping embed priority (asked 2026-08-22)

Raising `prio` on hub chunks or on the `embed_batch` job does **not** shrink
the window, and it's worth writing down why so it isn't retried.

Priority reorders a queue. During the blind window there is nothing in the
queue to reorder: the chunk exists, but no `embed_batch` job represents it yet.
Draining is already prompt — `job_inproc` claims one job per pass tick
(`cli/worker.py`, `limit=1`) and melchior logs `job_inproc claimed=1 ok=1`
every 30–90 s. Nor is capacity the constraint: the whole non-paper backlog was
~270 chunks (1 of them a finding) when this was measured, so a hub chunk is not
starved behind the 188k paper chunks.

The latency is **mint cadence, not queue position** — the materializer emits an
`embed_batch` roughly hourly (09:53, 10:17, 11:20, 12:09, 13:01, 14:14 on
2026-08-22). fi237847's chunk was created 14:50, missed that cycle, and embedded
~2 h later. Priority is the wrong axis; the fix has to create work sooner, not
rank it higher. (Job prio is ASC — lower = more urgent — and would matter if
`embed_batch` jobs were backing up behind other job types. They aren't.)

### Verification

Mint a hub, immediately search its own claim sentence, and assert it returns
itself. That test fails today.
