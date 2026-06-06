# Dreaming — memory consolidation + synthesis

Status: **proposed** (plan-first artifact; no code landed yet)
Author: (fill in)
Date: 2026-06-05

## Problem

The knowledge base has two related pressures that nothing addresses
today:

1. **Redundancy.** Memories (`kind='memory'`) accumulate as a flat,
   append-only stream. The same fact gets re-noted in slightly
   different words; a decision is refined across three entries; a
   question and its later answer live disconnected. Nothing merges
   them.
2. **No synthesis layer.** The corpus (papers, findings, gripes,
   memories) is a flat sea of chunks. There is no higher-altitude,
   "pre-chewed" layer that captures *"the interesting clusters we've
   been worried about lately"* — and no signal for what *recently
   mattered* to focus such synthesis on.

**Dreaming** is one worker with four modes. The taxonomy is
principled — it mirrors the leading hypothesis for what sleep does:
memory *consolidation* (Modes 1–3) and creative *recombination*
(Mode 4).

- **Mode 1 — consolidate** (reductive, memory-only): pick a memory,
  find its semantic neighbours, ask a strong supervising model whether
  a subset should be *merged*, and — on yes — replace them with a
  single better memory, **migrating all links** and retiring the
  originals.
- **Mode 2 — synthesize** (generative, cross-kind, non-destructive):
  find a salient region of embedding space, cluster it, summarize it
  with the LLM, and emit a new **dream-origin memory** that links back
  to its sources. RAPTOR-like; builds a recursive, searchable index
  *as* the link graph. Never deletes anything.
- **Mode 3 — navigate / TOC** (a *renderer* of Mode 2, not a separate
  worker): subcluster a region and emit a navigable table-of-contents
  memory — each entry a short gloss + pointers to representative
  sources. This is RAPTOR's local-clustering half; the same pass can
  emit both a Mode-2 summary (the node label) and a Mode-3 TOC (its
  children outline).
- **Mode 4 — inspire** (generative, speculative, fenced): take an
  *active* cluster (the "current issue") plus a few random/remote
  stimulus chunks (any kind), and ask the LLM whether there's a
  *realistic* way to apply the stimulus to the issue. On yes, emit a
  low-confidence `inspiration` memory. Remote-association / conceptual
  blending. Every attempt — accepted **or discarded** — is logged for
  later analysis (see §Dream log).

All modes share the finding-chase worker scaffold
(`src/precis/workers/chase.py`): a ref-level pass, one unit per
transaction, default-off LLM, audited through `ref_events`. A shared
**salience signal** on chunks (decayed access counter) drives
seed-selection and feeds search ranking; a shared **dream log** records
every attempt and its provenance for the feedback loop.

## Decisions (settled in discussion)

- **Mode 1 neighbours are semantic.** Memories get a `card_combined`
  chunk + embedding so "find adjacent memories" uses real cosine
  similarity, catching paraphrases a lexical title match would miss.
- **Memory-only consolidation; papers are never merged or deleted.**
  Mode 2 *reads* papers as cluster inputs but only ever *writes* new
  memories.
- **Autonomous, supervised by a strong model.** No separate human
  approval step; the *supervisor is the LLM* — an opus-class model with
  extended thinking — gated default-off behind `PRECIS_DREAM_LLM`. The
  `ref_events` log is the audit trail; soft-delete + a `supersedes`
  chain make every merge reversible at SQL.
- **Salience = lazy exponential decay, blended.** One float per chunk
  (`access_score`) + `last_seen`, decayed on touch; recency and
  frequency blended into a single number. Stored as **two new columns
  on `chunks`** (proper migration), updated in place.
- **Mode 2 clustering = retrieve-then-cluster.** ANN-retrieve a salient
  frontier (HNSW), then run real clustering (HDBSCAN/GMM) on that
  bounded subset. Never global GMM over the whole corpus (it's
  >500k chunks and growing). Clustering deps land behind an ADR when
  Mode 2 is built.
- **Retrieval: dreams blend into fused search with a boost** (not a
  hard memory-first gate), where the boost is **dream-recency relative
  to cluster activity** (see §Salience). Survives cold start; never a
  dead end (drill to sources via `summarises` links).
- **`new_tags` default = union of survivors' tags** (minus control
  tags); the LLM may prune.
- **Dream log keeps everything forever.** Every attempt is retained,
  including discards — no pruning, no retention window. Discards are
  simply marked `outcome='rejected'` and kept **invisible** (never
  surfaced in search or any normal view). The retained corpus is the
  dataset for "optimize the dreaming": fruitfulness rate, how far
  inspiration travels, which regions/kinds fertilize.
- **Dreams may reach outward (worker-mediated, unbounded).** A dream
  can request external searches — Semantic Scholar (abstracts + OA
  papers) and Perplexity (general knowledge). The LLM *proposes*
  searches in its JSON; the worker
  *executes* them via existing cached handlers and feeds results back,
  **iterating as many rounds as the dream wants** — dreaming is not
  latency-sensitive, so the prompt only *hints* at cost rather than
  enforcing a ceiling. The prompt advertises this as an option (see
  §External search). Not for Mode 1.

## What exists today (grounding)

- `MemoryHandler` is a `note_like` numeric-ref kind.
  `put(text=...)` → `insert_ref(kind='memory', title=text, meta={})`
  in `_numeric_ref.py::_create`. **No chunk is created**, so memories
  have no embeddings today. Memory `search` is lexical over
  `refs.title` (`search_refs_lexical`).
- Semantic search over chunks:
  `Store.search_blocks_semantic(query_vec, kind=..., scope_ref_id=...,
  max_distance=...)` (cosine via `chunk_embeddings`, excludes `ord<0`).
  Fused lexical+semantic via `search_blocks_fused` (RRF, k=60).
- HNSW ANN index already exists (`migrations/.../chunk_embeddings_hnsw`)
  — candidate retrieval scales to millions.
- `chunks` already has a `meta JSONB` column; chunk text is
  append-only with an embedding/summary cascade. **Non-text columns do
  NOT trigger that cascade** — so a salience column is safe to UPDATE
  in place (the "don't mutate chunks" rule is specifically about
  `chunks.text`).
- Links: `links (src_ref_id, src_chunk_id, dst_ref_id, dst_chunk_id,
  relation)` with a `UNIQUE ... NULLS NOT DISTINCT` index and a
  self-loop CHECK. Ops in `store/_links_ops.py` (`add_link`,
  `remove_link`, `links_for`).
- `soft_delete_ref(ref_id)` sets `deleted_at`; all read paths filter
  `deleted_at IS NULL`. Tombstones still render in link views with a
  "(deleted)" marker (provenance stays visible).
- `Relation` enum (`store/types.py`) + `relations` seed in
  `migrations/0001_initial.sql` are the source of truth.
  **`supersedes` does not yet exist.**
- Tags: `tags(namespace, value)` is generic data (no migration for new
  values), but `_KIND_ALLOWED_AXES["memory"] = frozenset()` rejects
  *closed* axes on memory. So a closed `DREAM:` axis would need code
  edits to `_CLOSED_VOCAB` + `_KIND_ALLOWED_AXES` (not SQL); an open
  tag or `meta.*` needs nothing.
- LLM via `call_claude_p(prompt, model=...)` (`utils/claude_p.py`);
  per-call model override + `--max-budget-usd` cap. Chase defaults to
  Haiku; dreaming passes an opus-class model.
- **External providers already exist as callable, cached paths:**
  - Semantic Scholar: `ingest/semantic_scholar.py::lookup_s2(title)`
    (search → metadata + abstract), `get_paper_by_id(doi/arxiv/s2)`;
    `workers/fetch_oa.py::_query_s2_openaccess(paper_id)` → OA PDF URL.
    Free at low volume.
  - Perplexity: cache-backed kinds `websearch` (~$0.001), `think`
    (~$0.005), `research` (deep) in `handlers/perplexity.py`, via
    `CacheBackedHandler` (parsed, embedded, stored as refs +
    `cache_state`). Needs `PERPLEXITY_API_KEY`. The `~$0.50` figure for
    `research` is the repo's own declared estimate
    (`cost_per_call_usd` in `perplexity.py`), not a measured price —
    verify against current Perplexity pricing if it matters; the cheap
    tiers are the default anyway.
  - Cache discipline: `cache_state` (provider, request_hash, TTL,
    cost) + `CACHE:`/`WATCH:` tags give dedup, idempotency, and cost
    tracking for free (`store/_cache_ops.py`).
- Worker scaffold: `run_finding_chase_pass` — claim
  `FOR UPDATE ... SKIP LOCKED`, one ref per tx, default-off LLM, write
  `ref_events`, mutate `meta`, re-emit `card_combined` via
  DELETE+INSERT, flip a tag. CLI registers ref-passes in
  `cli/worker.py` (see `fetch_oa` for a default-off pass).

## Salience signal (shared by both modes)

A per-chunk recency×frequency score that answers "what mattered
lately", feeding Mode 2 seed-selection and search ranking.

### Storage — two columns on `chunks` (migration)

```
ALTER TABLE chunks
  ADD COLUMN access_score DOUBLE PRECISION NOT NULL DEFAULT 0,
  ADD COLUMN last_seen    TIMESTAMPTZ;          -- NULL = never touched
```

Update **in place** on touch; no side table, no per-access append log
(an append log is unbounded at 500k+ read volume — rejected).

### Lazy exponential decay (no batch sweep)

```
λ = ln(2) / half_life_days                 # one tunable
decayed = access_score * exp(-λ * Δt_days) # Δt = now - last_seen

# on touch:  access_score = decayed + weight ; last_seen = now()
# on read:   compute `decayed` only (no write)
```

No daily decay job — decay is folded in on touch and computed at read.
Untouched chunks floor toward 0 (= forgotten), which is correct.
Half-life is the knob (`PRECIS_DREAM_HALFLIFE_DAYS`, ~7–30d). Proven
lineage: exponential time-decayed counters / forward decay (Cormode
2009), HN/Reddit "hotness", LFUDA caches.

### Write path — async/batched (hot-path threshold)

`thresholds.md` forbids new synchronous writes on `precis serve`'s
search path. So access events are **buffered and flushed in batches**
by a worker (or coalesced per request), not written inline with the
query. Destination is still the two columns above; only the *timing*
is deferred. Weight by access type (cite ≫ full-read > search-surface
impression).

### Importance prior (kept OUT of the counter)

Combine static/structural importance at seed-selection time, not in the
decaying scalar:

```
salience = decayed_access_score × importance_prior
importance_prior = f(link_degree, kind_weight, PRIO, human_verified,
                     citation_count)
```

## Mode 1 — consolidation (memory-only)

### Schema migration `0003_dreaming.sql`

Additive only:

- Salience columns on `chunks` (above).
- Seed `relations`: `supersedes` (inverse `superseded-by`) and
  `superseded-by` (inverse `supersedes`); mirror in the `Relation`
  Literal + `_INVERSE_RELATIONS` in `store/types.py`.

`supersedes` is deliberately distinct from `retracts` (retraction =
"this was wrong"; supersession = "absorbed into a better phrasing").
The original is **soft-deleted, not hard-deleted** — the `supersedes`
edge and `ref_events` audit point *at* the old row, so a hard delete
would orphan provenance and break reversibility.

### Memories become embeddable

Give each memory a synthetic `card_combined` chunk (`ord=-1`) holding
its text, so the embed worker vectorizes it and `search_blocks_semantic`
finds neighbours.

- **On create**: emit the card chunk in the same tx as the ref insert.
- **On edit/text change**: DELETE+INSERT the `ord=-1` row so the
  embedding cascade re-runs (never in-place UPDATE of `chunks.text`).
- **Backfill**: one-shot pass creates the missing card for existing
  live memories; the embed worker drains them.
- **Scope: `memory` only** for now (widen to other `note_like` kinds
  later if wanted).

### The worker `precis.workers.dream` (consolidation path)

`workers/dream.py` (orchestration) + `workers/_dream_llm.py`
(default-off opus hook + prompt).

**Claim.** Live memories due for a pass and not retired. Cooldown via
`meta.dreamed_at` (JSONB, no registry friction):
`memory refs WHERE deleted_at IS NULL AND not-recently-dreamed
ORDER BY ref_id LIMIT n FOR UPDATE ... SKIP LOCKED` (release locks,
then touch each in its own tx).

**Neighbour gather.** Lookup the seed's card vector, run
`search_blocks_semantic(kind='memory', exclude self, max_distance=floor,
limit=K)`. Build a candidate cluster with guards: min size 2, max size
capped (~6), distance floor (`PRECIS_DREAM_MAX_DIST`). No cluster →
mark dreamed, no-op.

**LLM supervision (default-off, opus-class).** Show cluster (id + text +
tags + links summary); strict JSON (full prompt template + parse/repair
policy in §Prompts & output contracts):

```json
{
  "merge": true | false,
  "merge_ids": [<int>, ...],       // subset of shown ids, >= 2
  "new_text": "<consolidated memory>",
  "new_tags": ["..."],             // default: union of survivors
  "reason": "<one sentence>"
}
```

Conservative: `merge=false` unless confident the subset says the same
thing / refines one another. LLM error or unparseable → no-op
(`mark dreamed, move on`), like chase tolerates `None`.

**Consolidate (one tx)** when `merge=true`:
1. Create the new memory (`insert_ref` + `card_combined` chunk + merged
   tags: union of survivors minus control tags, LLM may prune).
2. **Migrate links.** New `Store.migrate_links(old, new)`: for every
   link touching any `merge_id`, re-point the old endpoint to `new`
   (preserve relation + other endpoint), `INSERT ... ON CONFLICT DO
   NOTHING` to dedup against the unique index, drop self-loops, then
   DELETE the old rows.
3. Add `new --supersedes--> old` per merged id (auto-mirrors).
4. Stamp `meta.superseded_by = new_id` (+ `meta.dreamed_at`) on each
   old ref, then `soft_delete_ref(old)`. Originals stop surfacing;
   survivor's link view shows them as "(deleted)".

**Audit.** One `ref_events` row per pass (source `'dream'`): decision,
cluster ids, distances, merged-into id, link-migration counts, LLM
cost — like chase's `_flush_event`.

### Safety / idempotency

- Retired memories are soft-deleted + `dreamed`-stamped → not
  re-claimed; survivor enters cooldown.
- Reversible at SQL (`deleted_at=NULL` + the `supersedes` edges).
- Cost bounded by `--max-budget-usd` × cluster-size cap; gate off by
  default.

## Mode 2 — synthesis (cross-kind, non-destructive)

Generative RAPTOR-like layer. Reads any kind; writes only
`kind:dream`-tagged memories. Never deletes.

### Retrieve-then-cluster (scales past 500k)

Per pass:
1. **Seed selection.** Pick salient seeds.
   - *Warm:* high-churn regions (top chunks by decayed `access_score`).
   - *Idle:* dense, **undreamt** clusters (structure-driven) — regions
     with many chunks and no `summarises`-linked dream within ε of the
     centroid. This is also the **cold-start** answer: dreaming
     bootstraps from usage; when quiet, it pre-chews dense uncovered
     regions.
2. **Frontier retrieval (HNSW, scales).** For each seed, ANN top-k →
   union into a working set of a few thousand chunks. Never loads the
   full matrix.
3. **Cluster the working set (cheap).** HDBSCAN/GMM (soft assignment)
   over the few-thousand-vector subset — genuine structure, sub-second,
   memory-bounded regardless of corpus size. *(Adds `scikit-learn` /
   `umap` — ADR-gated cross-package threshold, taken when Mode 2 is
   built.)*
4. **Optional wildcards** (`PRECIS_DREAM_WILDCARDS=0..2`): add 1–2
   members sampled from a *distant* cluster (low centroid similarity)
   for grounded surprise; tag those dreams `DREAM:speculative` for easy
   pruning. (A synthetic random high-dim vector is noise — use a real
   distant chunk instead.)
5. **Synthesize salient clusters.** LLM summarizes; emit a dream-origin
   memory; link to sources; add temporal keywords (ISO week/month) so
   dreams are time-addressable.

> **Relation note:** there is no `summarises` relation in the enum
> today (closest: `derived-from`/`derived-into`, `generalises`). Mode 2
> either reuses `derived-from` (dream derived-from its sources) or adds
> a dedicated `summarises`/`summarised-by` pair in the Mode 2 migration.
> Recommendation: reuse `derived-from` initially; add `summarises` only
> if the distinction earns its keep. References to `summarises` below
> mean "the source-provenance relation, TBD".

### Index = the link graph (no tree table)

Don't persist cluster tables. The durable artifact is dream-origin
memories + their `summarises` links; clustering output is thrown away
each pass. Retrieval is plain pgvector search over chunks (dream cards
included) → collapsed-tree retrieval for free, scales with HNSW.

### Recursion (bottom-up, gets cheaper with height)

Dream memories are themselves embeddable memories, so a later pass
clusters *them* too. Level 0 (500k+) → frontier-only; level 1 (dream
memories, hundreds–thousands) → can cluster more globally; level 2+
→ tiny, fully global. Track height with `meta.dream_level`; children
via `summarises`. Cap depth + cooldown to avoid runaway.

### Cooldown & search boost — recency RELATIVE to cluster activity

A dream's freshness is judged against its cluster's own latest churn,
not wall-clock:

```
cluster_activity = max(last_seen) over cluster members   # decayed-weighted ok
dream_is_current = dream.created_at >= cluster_activity
staleness        = access accumulated in the region SINCE dream.created_at
```

- **(Re)dream scheduling:** trigger when `staleness` crosses a
  threshold — i.e. the region churned since the last covering dream.
  Not on a timer. (5-month-old cluster + 4-month-old dream = fresh,
  skip; 1-day-old churning cluster + 2-day-old dream = stale,
  re-dream.)
- **Search boost (option A):** dreams ride in `search_blocks_fused`
  with a recency×importance bump; the bump is high when
  `dream_is_current` and decays as the cluster churns past it, so stale
  dreams sink rather than mislead. Always expose `summarises` links so
  the agent can drill to primary sources (no dead ends; survives cold
  start). **Mode 4 inspirations are exempt from this boost** — see
  Mode 4.

## Mode 3 — navigate / TOC (renderer of Mode 2)

Not a separate worker. The same retrieve-then-cluster pass that
produces a Mode-2 summary can also **subcluster** the region (RAPTOR's
local step) and emit a **TOC dream-origin memory** rendered under a
`view='toc'` — reusing precis's existing TOC vocabulary
(`segment_toc` worker, `ref_segments`, `views=("toc", …)` on handlers),
but at cross-ref / region scope instead of per-ref.

- **Output:** a memory whose body is an outline; each entry =
  `<subcluster gloss>` + links to representative source chunks/refs
  ("expound with context" = gloss + drill-down, not a wall of prose).
- **Subcluster guards:** cap entry count; only expound subclusters
  above a size/salience floor so a TOC doesn't balloon.
- **One structured LLM call** returns the whole outline to start
  (per-subcluster gloss calls are a later, more granular option).
- **Shares everything else** with Mode 2: seeds, frontier retrieval,
  recursion (`meta.dream_level`), relative-recency cooldown, search
  boost.

Why a renderer, not a Mode-3 worker: standalone would duplicate the
entire frontier/cluster/cooldown machinery for what is just a second
output shape of the same clustering.

## Mode 4 — inspire (speculative, fenced)

The creative-recombination mode. Reuses the salience seed + retrieval
scaffold but inverts the roles of Mode 2's wildcard step:

- **Target = an active cluster** (warm / high-churn salience = "what
  we're worried about now").
- **Stimuli = a few random/remote chunks** (any kind — cross-kind is
  the point). Sampling knob: *pure-random* (max serendipity, low
  yield) vs *distance-stratified* (far-but-not-orthogonal; curated
  surprise, higher yield). Start distance-stratified
  (`PRECIS_DREAM_WILDCARDS` count; `PRECIS_DREAM_STIMULUS_MODE`).
- **LLM judgment, not summary:** "is there a *realistic* way to apply
  <stimulus> to <issue>? Say no by default." Batch several stimuli per
  prompt to amortize cost. Most attempts → no → discard (cheap).
- **On yes:** emit a low-confidence memory tagged `kind:inspiration` +
  `DREAM:speculative`, with a confidence field and links to *both*
  parents (issue cluster + stimulus) so the provenance — "this idea
  came from applying X to Y" — is traceable.

**Fencing (decided):** inspirations are NOT boosted in default search
and do not pollute authoritative results. They surface on explicit ask
or a dedicated view, and the `DREAM:speculative` tag makes pruning
trivial. Later Mode-1 consolidation or a human can promote the good
ones.

**Autonomy:** autonomous-write is acceptable *because* it's fenced and
non-destructive — a bad inspiration is inert clutter, never a
corruption. Cost is the main risk: tightest per-call budget, lowest
frequency, default-off.

## External search (dreams reaching outward)

Dreaming need not be closed-corpus. A dream can pull in *new* external
knowledge — to ground a speculative Mode-4 inspiration (has someone
already done this? what's the prior art?) or to fetch a reference a
Mode-2 cluster keeps citing but we don't hold. Mode 1 (internal dedup)
doesn't use it.

**Worker-mediated, not free browsing.** The model does *not* call the
internet directly. It **proposes** structured search requests; the
deterministic worker **executes** them through the existing cached
handlers, ingests results as normal refs (deduped, embedded), links
them to the dream, and re-prompts with the findings — **iterating as
many rounds as the dream wants**. Rationale: reuses the cache layer
(idempotent, cost-tracked, auditable), honours the AGENTS.md ingest
guarantees, and doesn't depend on `call_claude_p`'s own tool/web
access.

**Prompt contract.** The prompt advertises the option and the model may
emit (alongside its decision):

```json
{
  "searches": [
    {"provider": "s2" | "websearch" | "think" | "research",
     "query": "<text>",
     "reason": "<why this would change the verdict>"}
  ]
}
```

Guidance baked into the prompt: *search freely when it would help —
you can take your time. Prefer free Semantic Scholar and the cheap
`websearch` tier; the deep `research` tier costs a little more, so
reserve it for when it's clearly worth it.* (A hint, not a hard
limit.)

**Execution.**
- Provider → entry-point: `s2` → `lookup_s2` / `get_paper_by_id`
  (+ `_query_s2_openaccess` → `precis add` the OA PDF); `websearch` /
  `think` / `research` → the Perplexity handler `get`.
- **Unbounded rounds.** The dream searches iteratively until satisfied
  — no hard query cap or round limit. Cost is a *hint* in the prompt,
  not a ceiling.
- Cache hits are free; misses pay once and are reused thereafter, so
  repeated dreams over the same region get cheaper over time.
- Every external query (provider, cost, hit/miss, result ref ids) is
  recorded in the dream's `dream_log.verdict` and `ref_events`, and the
  fetched refs link back to the resulting dream for provenance.

**How a search actually runs (in-process, not over MCP).** The LLM's
`searches[]` is *data, not an executed call*. The model proposes; the
worker validates and executes. Execution reuses the **same in-process
entry the MCP server uses** — `runtime.dispatch('get', {'kind':
'websearch', 'q': ...})`, equivalently `hub.handler_for(kind).get(...)`
(for `s2`, the `lookup_s2` / `get_paper_by_id` → `precis add`
functions). MCP is only a transport shell over `runtime.dispatch`
(`mcp_modalities._read_resource` just calls it), and the worker is
already in the process — so it bypasses the transport: no self-MCP
round-trip, and **not** `call_claude_p`'s own tool/web access (rejected
for cache/audit/cost control). Each call returns a `Response` (rendered
body + `cost`) **and** persists a cached ref (refs + chunks +
cache_state) as a side effect, so the fetched knowledge is immediately
searchable and linkable. The worker folds a trimmed body into the next
round's `PRIOR SEARCH RESULTS` block, records `Response.cost` + the
created ref id in `dream_log`, and links the ref to the dream. This is
the same **propose / dispose** split that keeps the `decision` (merges,
inspirations) out of the model's hands — the model never writes.

**Cost & safety.** Default-off behind the same `PRECIS_DREAM_LLM` gate
plus a separate `PRECIS_DREAM_SEARCH` toggle. No hard budget ceiling
(unbounded by choice); the cheap tiers (free S2, ~$0.001 `websearch`)
are the default and the prompt nudges toward them, with `research`
reserved for when it's clearly worth it. A new top-level capability —
warrants its own ADR.

## Prompts & output contracts

All dream LLM calls go through the same `call_claude_p(prompt,
model=..., max_usd=...)` path the chase worker uses
(`utils/claude_p.py`), with `model` set to the opus-class
`PRECIS_DREAM_MODEL`. The shared mechanics, grounded in the existing
chase hooks (`workers/_chase_llm.py`):

- **Single-prompt, JSON-tail.** There is no separate system field —
  the persona + rules are the prompt preamble, and every prompt ends
  with *"Respond with EXACTLY ONE JSON object, nothing else:"* + the
  schema. `_parse_last_json_block` takes the rightmost balanced `{…}`,
  so a stray sentence of preamble is tolerated.
- **Stateless calls.** `--no-session-persistence` means no
  conversation memory between calls. The search loop is therefore
  **worker-driven**: each round the worker re-calls `call_claude_p`
  with a fresh prompt that *appends* prior search results (a
  `PRIOR SEARCH RESULTS` block); the model is not "continuing" a chat.
- **Context caps.** Chunk/excerpt text is truncated before
  interpolation (chase uses `[:4000]` / `[:1500]` / `[:200]`). Dream
  prompts never dump a full cluster — see input rendering below.

### Parse / validate / repair policy

Mirrors chase's `None`-tolerance, made explicit:

1. **Transport / parse failure** — `ClaudePError` (non-zero exit,
   timeout, *no parseable JSON block*) → **no-op**: stamp
   `meta.dreamed_at`, log a `dream_log` row with `outcome='error'`,
   move on. Never partially applies.
2. **Parsed but schema-invalid** — required key missing, wrong type,
   or a semantic guard fails (e.g. Mode 1 `merge_ids` not a length-≥2
   subset of the shown ids; Mode 4 `confidence` out of `[0,1]`) →
   treat as **no-op** too (`outcome='rejected'`, reason
   `'schema_invalid'`). Conservative by default.
3. **No automatic repair re-prompt** in v1 (matches chase, which does
   not retry). A single repair round is a future option behind a flag,
   not baseline — it costs a second opus call for a rare event.

So *"unparsable"* (the Mode 1 term) = case 1 or 2: the worker could not
recover a valid, schema-conforming object, so it does nothing and lets
the memory cool down.

### Input rendering & token budget

A Mode 2 frontier is thousands of chunks; it is never serialized whole.
The worker renders a **bounded, representative view**:

- Cluster members → a table of `(id, kind, tags, excerpt[:500])`,
  capped at the **N most central** rows (nearest centroid) plus a few
  peripheral ones; remaining count summarized as `"+M more"`.
- Mode 1 shows the focal memory + its neighbours in full (memories are
  short).
- The shown `id`s are the *only* handles the model may cite back in
  `source_ids` / `merge_ids` / `stimulus_id`; the worker maps them to
  real ref/chunk ids and rejects any id it didn't show.

### Shared SEARCH PROTOCOL block (Modes 2 & 4)

Interpolated into Mode 2/4 prompts (not Mode 1):

```text
SEARCH PROTOCOL (optional):
You may gather external knowledge before deciding. Providers:
  s2        - Semantic Scholar (free): papers, abstracts, OA PDFs
  websearch - quick web answer (~$0.001)   [prefer this]
  think     - deeper analytical answer (~$0.005)
  research  - multi-step deep research (costs a little more; reserve
              it for when it's clearly worth it)
Search freely when it would change your decision - you can take your
time; prefer the cheap tiers. To search, return "searches" non-empty
and "decision": null. The worker runs them and re-prompts you with the
results. When you are done, return "searches": [] and a full
"decision".
```

The envelope for searchable modes is uniform:

```json
{
  "searches": [{"provider": "...", "query": "...", "reason": "..."}],
  "decision": { /* mode-specific, see below */ } | null
}
```

Worker loop: `searches` non-empty → execute via the §External-search
entry-points, append a `PRIOR SEARCH RESULTS` block, re-prompt
(unbounded rounds). `searches` empty + `decision` non-null → apply.

### Mode 1 — consolidate (no search)

```text
You are consolidating a personal knowledge base's MEMORY notes. You
are shown a focal memory and its nearest semantic neighbours. Decide
whether a SUBSET of them say the same thing or refine one another and
should be MERGED into one better memory.

Be CONSERVATIVE: default merge=false unless you are confident the
subset is redundant or one clearly refines another. Never merge notes
that carry distinct, independently useful facts.

MEMORIES (id, tags, text):
{cluster_table}

Respond with EXACTLY ONE JSON object, nothing else:
{{
  "merge": true | false,
  "merge_ids": [<int>, ...],   // subset of shown ids, length >= 2
  "new_text": "<consolidated memory; \"\" when merge=false>",
  "new_tags": ["..."],          // default: union of merged tags
  "reason": "<one sentence>"
}}
```

### Modes 2 & 3 — synthesize + TOC (one call)

The synthesis pass emits the Mode-2 summary and the Mode-3 TOC together
(Mode 3 is a renderer, not a separate call):

```text
You are writing a higher-level SYNTHESIS over a cluster of items
(papers, findings, notes) from a knowledge base. Produce (1) a concise
"dream" memory capturing what this region is about and why it mattered
lately, and (2) a table of contents breaking the region into
sub-themes.

Cite sources only by their shown id. Invent nothing unsupported by the
shown items; if the cluster is incoherent, say so in "summary" and
return few or no toc entries.

{SEARCH_PROTOCOL}

ITEMS (id, kind, tags, excerpt):
{cluster_table}

{prior_search_results}

Respond with EXACTLY ONE JSON object, nothing else:
{{
  "searches": [ ... ],
  "decision": {{
    "summary": "<the dream memory text>",
    "tags": ["..."],
    "source_ids": [<int>, ...],
    "toc": [
      {{"label": "<sub-theme>",
        "gloss": "<one or two sentences>",
        "source_ids": [<int>, ...]}}
    ]
  }} | null
}}
```

### Mode 4 — inspire (search + judgment)

```text
You are looking for non-obvious but REALISTIC cross-applications. You
are shown a CURRENT ISSUE (an active cluster the user is working on)
and one or more unrelated STIMULUS items from elsewhere in the
knowledge base. For each stimulus, decide whether there is a
realistic, concrete way to apply it to the issue.

Be skeptical: say no by default. A forced or generic analogy is a no.
Return apply=true only when you can name a specific, plausible use.

{SEARCH_PROTOCOL}

CURRENT ISSUE:
{issue_summary}

STIMULI (id, kind, excerpt):
{stimulus_table}

{prior_search_results}

Respond with EXACTLY ONE JSON object, nothing else:
{{
  "searches": [ ... ],
  "decision": {{
    "ideas": [
      {{"stimulus_id": <int>,
        "apply": true | false,
        "idea": "<concrete application; \"\" when apply=false>",
        "confidence": 0.0,
        "reason": "<one sentence>"}}
    ]
  }} | null
}}
```

Each `idea` with `apply=true` becomes one fenced `inspiration` memory;
`apply=false` ideas are logged to `dream_log` (`outcome='rejected'`)
and never written as memories.

## Dream log (telemetry & feedback loop)

`ref_events` is keyed to a ref, so it cannot record **discarded**
ideas (no memory was created). Mode 4 (and the speculative paths of
all modes) need a dedicated, low-volume log — the substrate for
closing the loop on *which stimuli actually fertilize*.

```sql
CREATE TABLE dream_log (
  attempt_id         BIGSERIAL PRIMARY KEY,
  mode               TEXT NOT NULL,        -- consolidate|synthesize|toc|inspire
  created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  outcome            TEXT NOT NULL,        -- accepted|rejected|noop|error
  confidence         DOUBLE PRECISION,     -- LLM self-report, nullable
  seed_ref_id        BIGINT,               -- the issue/seed (nullable)
  target_chunk_ids   BIGINT[],             -- active-cluster members
  stimulus_chunk_ids BIGINT[],             -- fertilizing samples (Mode 4)
  stimulus_dist      DOUBLE PRECISION[],   -- per-stimulus cosine dist from target centroid
  result_ref_id      BIGINT REFERENCES refs(ref_id),  -- created memory if accepted
  model              TEXT,
  cost_usd           DOUBLE PRECISION,
  verdict            JSONB                 -- raw parsed LLM JSON (reason, …)
);
```

- **Keep everything forever; bad ones just go invisible.** No pruning,
  no retention window. Rejected attempts stay as
  `outcome='rejected'` rows and never surface in search or normal
  views — they exist only for post-analysis. This is the dataset for
  "optimize the dreaming".
- **Records every attempt**, including rejections — that's the whole
  point: the rejected (stimulus, target) pairs are negative training
  signal.
- **Analytic dimensions** the schema makes queryable: fruitfulness
  rate (`accepted / total` per mode), *how far inspiration travels*
  (`stimulus_dist` distribution split by outcome — do accepts come
  from near or far?), and which (stimulus kind × distance bucket ×
  target region) combinations pay off.
- **Provenance even for discards:** `target_chunk_ids` /
  `stimulus_chunk_ids` capture "the notes we used" without needing a
  memory to hang links on.
- **Feedback loop:** offline analysis of acceptance rate by
  (stimulus kind, distance bucket, target region) tunes the
  fertilizer-selection policy — settling pure-random vs
  distance-stratified, and which kinds fertilize which targets. Could
  later feed a lightweight scorer; for now it's descriptive stats.
- **Volume is fine:** the speculative modes are gated, default-off,
  and low-frequency, so a per-attempt row here is nothing like the
  rejected per-access access-log (that was unbounded by *read* volume;
  this is bounded by *dream* volume).
- Indexes: `(mode, outcome, created_at)`; optional GIN on
  `stimulus_chunk_ids` for "what did stimulus X ever inspire".

## CLI wiring

Register the passes in `cli/worker.py` as ref-passes, default-off in
the normal rotation (like `fetch_oa`). One `--only dream` entry with a
`--dream-mode {consolidate,synthesize,inspire}` switch (Mode 3 rides on
`synthesize` as an output option, not its own mode). Env knobs:
`PRECIS_DREAM_LLM` (gate), `PRECIS_DREAM_MODEL` (opus-class default),
`PRECIS_DREAM_MAX_DIST`, `PRECIS_DREAM_HALFLIFE_DAYS`,
`PRECIS_DREAM_WILDCARDS`, `PRECIS_DREAM_STIMULUS_MODE`, cluster-size +
staleness thresholds.

## Test plan

- **Salience:** lazy-decay math (half-life, floor-to-zero), in-place
  update, batched-flush coalescing, read-side no-write.
- **Mode 1:** `Store.migrate_links` (re-point, dedup-on-collision,
  self-loop drop, in+out, chunk-pos); card-chunk emission on
  create/edit + backfill; neighbour gather guards; mocked-LLM
  (`PRECIS_CLAUDE_BIN` stub) merge=false no-op vs merge=true full
  consolidation (link counts, survivor surfaces / originals don't,
  `supersedes` edges); `ref_events` shape.
- **Mode 2:** seed selection (warm churn vs idle dense-undreamt);
  frontier retrieval bound; clustering on a fixture subset; dream-origin
  memory + source links + temporal keywords; cooldown by
  source-overlap / relative-recency; recursion level tagging; search
  boost ordering + drill-down.
- **Mode 3:** subcluster decomposition; TOC renderer output shape
  (entry gloss + source links); entry-count cap + size/salience floor;
  `view='toc'` rendering.
- **Mode 4:** stimulus sampling (pure-random vs distance-stratified);
  mocked-LLM accept vs reject; accepted → `kind:inspiration` +
  `DREAM:speculative` memory with both-parent links; **fenced** (absent
  from default search boost); every attempt (accept/reject/noop/error)
  writes a `dream_log` row with target/stimulus chunk ids.
- **Dream log:** row written on all outcomes incl. discards;
  discard rows carry stimulus/target ids and no `result_ref_id`;
  acceptance-rate aggregation query.
- **Prompts / parsing:** `ClaudePError` → `outcome='error'` no-op;
  schema-invalid (missing key, bad type, `merge_ids` not a ≥2 subset
  of shown ids, out-of-range `confidence`) → `outcome='rejected'`
  no-op; id-handle rejection (model cites an id it wasn't shown);
  search-loop terminates when `searches=[]` + `decision` non-null;
  worker appends `PRIOR SEARCH RESULTS` across stateless rounds.

## Definition of done (per AGENTS.md)

- Plan reviewed (this doc).
- ADRs in `docs/decisions/`: (a) new `supersedes` relation +
  autonomous-but-LLM-supervised policy; (b) salience model + columns
  on `chunks` + the `dream_log` table; (c) Mode 2/3 clustering
  dependency (`scikit-learn`/`umap`) — taken when synthesis is
  implemented, not before; (d) Mode 4 inspiration policy (fenced,
  speculative-tagged, logged); (e) external-search capability
  (worker-mediated, cached, budget-capped, default-off).
- `0003_dreaming.sql` applies cleanly to a fresh DB (salience columns,
  `supersedes` relation, `dream_log` table); only the new file pending
  under `precis migrate --dry-run`.
- Mode 1 implemented end-to-end (memory card + backfill + worker);
  `--only dream` has `--help`, an integration test, a README line.
- Modes 2/3/4 may land in later increments behind their own ADRs +
  flags.
- Full check green (`ruff check`, `ruff format --check`, `mypy`,
  `pytest`); version bump + `CHANGELOG` entry.

## Suggested sequencing

1. Salience columns + lazy decay + batched flush (useful on its own for
   search ranking).
2. Memory card chunk + backfill + `supersedes` migration + `dream_log`
   table.
3. Mode 1 consolidation worker (full reductive loop).
4. Search boost for the memory layer (option A).
5. Mode 2 synthesis (+ Mode 3 TOC renderer) behind its own ADR +
   clustering deps.
6. Mode 4 inspiration (cheapest to bolt on once seeds + dream_log
   exist; default-off, fenced).
7. External search behind `PRECIS_DREAM_SEARCH` + its ADR: wire S2 +
   Perplexity into the search-request loop.

## Open questions for the reviewer

1. Access-event weights (cite / read / search-impression) — starting
   values?
2. Mode 2 cooldown: pure relative-recency, or also a hard source-set
   overlap ceiling?
3. Mode 4 stimulus default: pure-random vs distance-stratified, and how
   many stimuli per prompt?
4. Half-life, distance floor, cluster-size, staleness thresholds —
   pick conservative defaults and tune from `dream_log` / `ref_events`
   telemetry.

_Resolved:_
- `dream_log` retention — keep everything forever; discards marked
  `rejected` and kept invisible (analysis-only).
- External search — **unbounded** rounds (dreaming can wait); cost is a
  prompt hint, not a ceiling; cheap tiers (S2, `websearch`) default,
  `research` reserved. (Patents deferred — kind exists but currently
  unavailable.)
