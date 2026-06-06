# Dreaming — agentic memory consolidation + synthesis

Status: **proposed** (plan-first artifact; no code landed yet)
Author: (fill in)
Date: 2026-06-05 (agentic re-architecture 2026-06-06)

> **Architecture note (2026-06-06).** Dreaming is now a **full-agentic**
> loop: a thin worker hands an opus-class model a turn/cost budget and a
> connection to precis's own MCP tools, and lets it roam. The four
> "modes" below are no longer separate workers — they are *behaviors*
> the one agent may choose. The intelligence lives in the **navigation
> tools** (§Dream navigation tools) and the agent loop (§The dreaming
> agent), not in worker control flow. The earlier per-mode JSON prompt
> contracts are retained at the end as an **optional deterministic
> fallback**, not the primary path.

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

**Dreaming** is a single **agent**, not four workers. A thin worker
launches an opus-class model connected to precis's MCP surface, gives
it a turn/cost budget, and one instruction: *roam the knowledge base
and improve it, or do nothing.* The taxonomy below is principled — it
mirrors the leading hypothesis for what sleep does: *consolidation* and
creative *recombination* — but each is now a **behavior the agent may
choose**, reached by the same navigation tools, not a separate code
path:

- **Consolidate** (reductive): notice memories that say the same thing;
  write one better merged memory, **migrate links**, retire the
  originals via `supersedes` + soft-delete.
- **Synthesize** (generative, non-destructive): notice a dense, active
  region; write a **dream-origin memory** summarizing it, linked to its
  sources. RAPTOR-like; the link graph *is* the index.
- **Navigate / TOC**: emit a navigable outline over a region (a
  rendering of synthesize, not a separate behavior).
- **Inspire** (speculative, fenced): pull a far-away or *opposite*
  concept against an active cluster; if a realistic application exists,
  write a low-confidence `inspiration` (tagged `DREAM:speculative`).
  Remote-association / conceptual blending.
- **Acquire** (outward, gated): notice a paper the corpus keeps bumping
  into but doesn't hold; mint a stub so the **existing** fetch pipeline
  auto-grabs it if open-access, else parks it on the required-papers
  backlog. The dream only mints the stub — it never ingests inline.

**Guiding philosophy (decided).** Memories are *low-value and
reversible*, and the model is clever — so **trust it, and treat NO
ACTION as a first-class, encouraged outcome.** A run that reads around
and concludes nothing is worth changing is a success, not a waste. "If
in doubt, do nothing" is the default.

Shared infrastructure under the agent: a **salience signal** on chunks
(what mattered lately) powering the cluster-navigation tools; a
**`dream_log`** recording every run + a saved transcript for the
feedback loop; **additive-only writes** through the normal MCP verbs so
a bad dream is inert clutter, never corruption.

## Decisions (settled in discussion)

- **Consolidation neighbours are semantic.** Memories get a
  `card_combined` chunk + embedding so `search(like=...)` uses real
  cosine similarity, catching paraphrases a lexical title match misses.
- **Link model unchanged; memory = ref + 1 card chunk.** A memory is a
  `ref` with a single `card_combined` chunk (`ord=-1`) for its body.
  Links stay **ref-anchored** (ref→ref, with `chunk_id` an optional
  *body* locus only — never a card, since cards are re-emitted via
  DELETE+INSERT). So a memory's links are keyed on its stable `ref_id`
  and always surface, regardless of card churn. No chunk→chunk rewrite,
  no migration. (Soft-delete is ref-only — `chunks` has no `deleted_at`.)
- **Memory-only consolidation; papers are never merged or deleted.**
  Synthesis *reads* papers as cluster inputs but only ever *writes* new
  memories.
- **Full-agentic, turn/cost-bounded.** No worker-coded mode dispatch and
  no per-call JSON contract on the hot path: a thin worker launches an
  opus-class model connected to the precis MCP server, capped by
  `--max-turns` (large-ish is fine — dreaming isn't latency-sensitive)
  and `--max-budget-usd`, gated default-off behind `PRECIS_DREAM_LLM`.
  The agent reads, decides, and writes through ordinary MCP verbs.
- **The navigation tools are the product, not the prompt.** The cleverness
  lives in a few MCP tools (active/unvisited clusters; near / far /
  opposite retrieval via a similarity knob) — see §Dream navigation
  tools. The prompt is deliberately minimal: *pick something, do the
  magic, or do nothing.*
- **Additive-only writes; no-op is success.** The agent may `put` new
  memories and `link` / `tag` existing ones; destructive consolidation
  (delete + link-migration) is the one behavior that still routes
  through a guarded `supersedes` path, not a raw `delete`. Soft-delete +
  the `supersedes` chain keep every merge reversible at SQL. Doing
  nothing is a valid, encouraged outcome.
- **Every run leaves a transcript.** The full agent tool-call transcript
  is written to a file outside the tree; `dream_log` keeps a pointer +
  summary. That is the audit trail (alongside `ref_events` for the
  writes themselves).
- **Salience = two quantities, not one.** *Access salience* `A` (lazy
  exponential decay of recency×frequency) is **stored** as two `NOT NULL`
  columns on `chunks` (`access_score`, `last_seen`), updated in place;
  ingest seeds it (cold-start for free), dream-actor reads don't.
  *Target-worthiness* `worth = Â·prior·(1−β·C)` is **derived** per region
  at cluster time — the coverage gate `C` doubles as the cooldown. See
  §Salience for the formulae and starting values.
- **Cluster tool = retrieve-then-cluster.** ANN-retrieve a salient
  frontier (HNSW), then run real clustering (HDBSCAN/GMM) on that
  bounded subset. Never global GMM over the whole corpus (it's
  >500k chunks and growing). Clustering deps land behind an ADR when
  the cluster tool is built.
- **Retrieval: dreams blend into fused search with a boost** (not a
  hard memory-first gate), where the boost is **dream-recency relative
  to cluster activity** (see §Salience). Survives cold start; never a
  dead end (drill to sources via `derived-from` links).
- **`new_tags` default = union of survivors' tags** (minus control
  tags); the LLM may prune.
- **Dream log keeps everything forever.** One row per run, retained
  including `noop` runs — no pruning, no retention window. Nothing in it
  surfaces in search or any normal view; it's analysis-only. The
  retained log + transcripts are the dataset for "optimize the
  dreaming": fruitfulness rate, behavior mix, which regions fertilize.
- **Search spans all kinds by default.** `search` already supports
  cross-kind fan-out (`kind='*'` / `'all'` / `'paper,memory'`,
  RRF-fused) — the agent uses it directly. `get` stays single-kind
  (it's id-addressed); the new `cluster` kind is reached the same way.
- **One similarity knob: near / ring / opposite.** `search` grows
  `target` (cosine, default ~1) + `tol` and a `like=<id>` seed, so the
  agent can ask for *adjacent* (target~1), a *ring* of moderately-
  related ideas (target~0.5), or the *opposite pole* (target=-1). See
  §Dream navigation tools for the (feasible) mechanics.
- **Dreams may reach outward.** Semantic Scholar and Perplexity are
  already MCP kinds (`websearch` / `think` / `research`, cached), so an
  agentic dream simply calls them like any other tool — no special
  worker plumbing. Cost is a prompt *hint* (prefer free S2 + cheap
  `websearch`; reserve deep `research`), bounded by the run's overall
  `--max-budget-usd`. Gated behind `PRECIS_DREAM_SEARCH`.
- **Dreams may acquire missing papers (auto-fetch, gated).** When a
  region keeps citing a paper we don't hold, the dream mints a stub via
  a guarded `acquire` tool; the existing `fetch_oa` worker auto-grabs an
  OA copy if one exists, otherwise the stub waits on the
  `precis stubs` required-papers backlog. Heavy ingest runs in the
  normal worker rotation, never in the dream turn. Gated behind
  `PRECIS_DREAM_ACQUIRE`.

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
  Haiku; dreaming passes an opus-class model. For the **agentic** path
  the same `claude` binary is launched with an MCP server config and
  `--max-turns` (Claude Code supports both); the worker captures
  stdout/session as the transcript.
- **Cross-kind search already exists.** `runtime._resolve_kind` /
  `_dispatch_cross_kind` fan a `search` across every
  `supports_search_hits` kind and RRF-fuse the streams; `kind='*'`,
  `'all'`, and comma-lists (`'paper,memory'`) are already accepted
  (`runtime.py`). So "search all kinds that support it" needs no new
  surface \u2014 the agent uses it as-is.
- **MCP is a thin shell over `runtime.dispatch`.** Every MCP verb (and
  every resource read) routes through `runtime.dispatch(verb, args)`
  (`mcp_modalities._read_resource`), which hits the same handlers a
  worker can call in-process. New navigation tools therefore land as
  ordinary handler verbs/views and are reachable both over MCP (for the
  agent) and in-process (for tests / deterministic fallback).
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
- **Acquire pipeline already exists end-to-end.** The chase worker
  mints **stub paper refs** when a citation resolves to a paper we don't
  hold (`chase._resolve_or_create_stub`: probe `ref_identifiers`, else
  `insert_ref(kind='paper', meta={set_by})` + register every id —
  idempotent via identifier-collapse). A stub is a `paper` ref with
  `pdf_sha256 IS NULL`. The **`fetch_oa` worker**
  (`claim_stubs_to_fetch`) then auto-claims *any* such stub carrying a
  DOI/arXiv/S2 id and cascades Unpaywall → arXiv → S2; on `fetch_ok` the
  PDF lands in the watch inbox → `precis add` promotes the stub to a
  full paper. No OA copy → `no_oa_version`, the stub stays in the
  backlog that `precis stubs` renders. So "auto-fetch if possible, else
  a required-papers list" already exists — a dream just has to mint the
  stub.
- Worker scaffold: `run_finding_chase_pass` — claim
  `FOR UPDATE ... SKIP LOCKED`, one ref per tx, default-off LLM, write
  `ref_events`, mutate `meta`, re-emit `card_combined` via
  DELETE+INSERT, flip a tag. CLI registers ref-passes in
  `cli/worker.py` (see `fetch_oa` for a default-off pass).

## Salience signal (shared by all behaviors)

"Salience" is really **two** quantities, and conflating them is the bug
in the original one-liner:

- **Access salience `A`** — a per-chunk recency×frequency score, *stored*
  on `chunks`. "What got attention lately."
- **Target-worthiness `worth`** — a per-*region* score, *derived* at
  cluster time, that decides where to dream: high `A`, but discounted
  where we *already* dreamed recently.

They pull opposite ways on dream-recency, so they must stay separate:

- **Seed-selection** wants high `A`, **low** recent-dream coverage — go
  where attention is, that we *haven't* just synthesized.
- **Search ranking** wants the opposite: a **recent** dream is a fresh
  summary, so it should float **up**. Same "dream age", opposite sign
  (search boost is specified in §Retrieval, not here).

### Storage — two columns on `chunks` (migration, backfilled)

```sql
ALTER TABLE chunks
  ADD COLUMN access_score DOUBLE PRECISION NOT NULL DEFAULT 0,
  ADD COLUMN last_seen    TIMESTAMPTZ      NOT NULL DEFAULT now();

-- backfill: treat ingest as the first access, at the chunk's birth
UPDATE chunks
   SET last_seen    = created_at,
       access_score = :w_ingest;   -- raw seed; read-time decay does the rest
```

Both columns are **`NOT NULL`** and the existing corpus is backfilled,
so no row ever lacks a value — **no NULL guard, no `CASE`** in the read
path. Updated **in place** on touch; no side table, no per-access append
log (unbounded at 500k+ read volume — rejected).

### Access salience `A` — lazy exponential decay (no batch sweep)

```
λ_access = ln(2) / HALF_LIFE_ACCESS_DAYS
A = access_score * exp(-λ_access * Δt_days)   # Δt = now - last_seen

# on external touch:  access_score = A + weight(access_type) ; last_seen = now()
# on read:            compute A only (no write)
```

No daily decay job — decay folds in on touch and is computed at read.
Untouched chunks floor toward 0 (= forgotten). Proven lineage:
time-decayed counters / forward decay (Cormode 2009), HN/Reddit
"hotness", LFUDA caches.

### Ingest is the first access (cold-start for free)

Acquiring a paper is a deliberate act of interest, so **ingest seeds
`A`** rather than being a separate freshness term — cold-start falls out
of the one decay mechanism, no extra coefficient. The seed is
**source-weighted** (`w_ingest`): a human `precis add` ≫ a dream
`acquire` ≫ a bulk/stub backfill. Storing the raw seed with
`last_seen = created_at` is identical to "ingest was a touch at birth",
so a fresh paper starts warm and cools if nobody engages — i.e.
**dreaming naturally drifts toward new material**, bounded by sampling
and the coverage gate below.

### Dream reads do NOT count as access (feedback-loop guard)

`A` must mean **external** attention. If the dreamer's own traversal
bumped `A`, regions it wandered into would heat up and attract more
dreaming — an echo chamber. So the access-accrual path **excludes the
dream actor's reads** (`weight = 0`, filtered on `set_by`). Two
carve-outs: a dream's **writes** are new content and get the ingest seed
(at a *low* `w_ingest`, so dreams don't make their own output hot); and
**later human access** to a dream-written memory *does* count — that is
the fruitfulness signal.

### Write path — keep it cheap (off the response path)

Salience writes are **lossy-tolerant**: a dropped bump barely moves a
fuzzy, decaying score. So the only real constraint is `thresholds.md`'s
"no new synchronous writes on `precis serve`'s search path" — satisfied
by a plain **fire-and-forget** in-place UPDATE after the response is
sent (best-effort; drop on contention or failure). No buffer, no
flush-worker, no coalescing — that's a later optimization *if* write
volume ever warrants it, not v1.

### Importance prior (structural, kept OUT of the counter)

Static/structural importance is a **separate multiplicative factor**
applied at seed-selection, never mixed into the decaying scalar:

```
prior = f(link_degree, kind_weight, PRIO, human_verified, citation_count)
```

### Ref-level rollup

`A` lives on chunks, but dreaming targets *regions* and ranking
sometimes wants a *document* number. Roll up:

```
A(ref) = mean over its chunks of A  +  small_mass_bonus(n_chunks)
```

Mean (size-neutral) plus a mild `ln(1+n)` mass bonus so a substantive
paper outweighs a one-line note without letting the biggest blob always
win. For a 1-chunk memory this is just its chunk's `A` (identity).

### Target-worthiness — `worth = A · prior · (1 − β·C)`

Multiplicative, so coverage `C` acts as a **bounded novelty gate**
rather than a unit-mismatched subtraction:

```
worth(R) = Â(R) · prior(R) · (1 − β · C(R))
```

- **`Â(R)`** — region access salience (rollup above), **normalized
  across the candidate set** (divide-by-max) so the sampling temperature
  is stable run-to-run.
- **`(1 − β·C)`** — novelty gate in `[1−β, 1]`. `β=1` ⇒ a fully-covered,
  unchanged region drops to 0 (hard cooldown); `β<1` leaves a floor so a
  red-hot region can still win.

### Coverage `C` — the cooldown, unified (two tiers, change-gated)

The "don't dream the same region back-to-back" cooldown **is** `C` — no
separate mechanism. For a candidate centroid `c`:

```
C(R) = clip01( max over prior dream-activity d near c of
               w_tier(d) · cos(c, emb_or_centroid(d)) · exp(-λ_dream · age_days(d))
               · change_gate(R, d) )
```

- **Two tiers (`w_tier`).** A region we *synthesized* (a dream memory
  exists near `c`) gets a **strong, long** cooldown; a region we merely
  *examined and no-op'd* (from `dream_log.seed_clusters`, even with no
  write) gets a cooldown that **escalates with consecutive no-ops**.
  - **Why escalate.** Without it, a *persistently salient but barren*
    region — the agent keeps looking, keeps declining — stays top-`worth`
    (no synthesis ever marks it covered), floats back up, and burns
    budget on the same dead end. Sampling only lowers the frequency; it
    doesn't stop the waste.
  - **Backoff.** Count consecutive no-op visits to the region (already in
    `dream_log`): 1st no-op → brief skip; repeated no-ops → geometric
    growth so `C → 1`, which under `β=1` drives `worth → 0` and exiles a
    truly dead region. The `change_gate` (below) resets the count — new
    material is a fresh reason to look, so an exiled region reopens the
    moment it changes.
- **Distance × recency.** Close + recent ⇒ high `C`; the `exp(-λ_dream)`
  term auto-expires the cooldown as the dream ages.
- **`change_gate`.** If `R` gained members *after* dream `d`, shrink
  `d`'s contribution (∝ fraction unchanged) — new material reopens a
  region even if recently dreamed. This is `view='unvisited'` made
  smooth.

### Seed selection — sample, don't argmax

Pick the region by **softmax sampling over `worth`** (temperature `τ`),
not strict max, so dreaming *explores* instead of hammering the single
top region every run.

### Definitions & starting values (the knobs)

| symbol | meaning | start | env / note |
|---|---|---|---|
| `λ_access` | access decay rate | half-life **14 d** | `PRECIS_DREAM_HALFLIFE_ACCESS_DAYS` (7–30) |
| `λ_dream` | coverage decay rate | half-life **30 d** | `PRECIS_DREAM_HALFLIFE_DREAM_DAYS`; longer than access |
| `β` | coverage suppression | **1.0** (hard gate) | drop to ~0.85 to soften; *intent, don't sweep* |
| `τ` | softmax temperature | tune so top region ≈ **2–3×** median pick-prob | |
| `w_ingest` | ingest seed, by source | human **1.0** / dream-acquire **0.5** / bulk **0.2** | dream *write* seed **0.2** |
| access weights | per touch type | cite **1.0** / full-read **0.5** / impression **0.1** | dream-actor read **0.0** |
| `w_tier` | cooldown strength | synthesized **1.0** / examined-noop **0.3 × backoff** | escalates with consecutive no-ops |
| mass bonus | rollup size term | `0.1 · ln(1 + n_chunks)` | mild |

All starting values; the genuine tunables are the **two half-lives** and
`τ`. `β` and the weights are set by *intent*, not parameter sweep.

## The dreaming agent (loop, prompt, budget)

The worker is **thin**. Per scheduled run it:

1. Checks the gate (`PRECIS_DREAM_LLM`). No separate cooldown — "don't
   dream the same region back-to-back" is the coverage gate `(1−β·C)`
   baked into seed-`worth` (see §Salience).
2. Launches the `claude` binary connected to the precis MCP server,
   with `model=$PRECIS_DREAM_MODEL` (opus-class), `--max-turns`
   (large-ish; \u00a7below) and `--max-budget-usd`.
3. Feeds the **minimal prompt** below and lets the agent drive: it calls
   precis tools to explore and, if warranted, to write.
4. On exit (agent stops, or turns/budget exhausted) captures the
   tool-call transcript to a file and writes one `dream_log` row
   (outcome, cost, turns, transcript path, any `result_ref_id`s).

The agent's whole job is to *optionally* leave the corpus a little
better. It is not orchestrated through modes; the behaviors
(consolidate / synthesize / TOC / inspire) emerge from which tools it
chooses.

### The prompt (deliberately minimal)

```text
You are dreaming over a personal knowledge base. Your job is to improve
it a little — or to do nothing. Doing nothing is a perfectly good
outcome; bias strongly toward LESS.

EXPLORE (read freely, no cost worry):
  - get(kind='cluster', view='active')      regions that mattered lately
  - get(kind='cluster', view='unvisited')   dense regions never dreamt
  - get(kind='cluster', id=N)               a region's members
  - search(kind='*', q=... | like=<id>, target=, tol=)
        target~1  adjacent ideas      target~0.5  a "ring" of related-
        but-different ideas           target=-1   the opposite pole
  - external: websearch / think / research  (only if it clearly helps;
        prefer the cheap tiers)

ACT only if it clearly helps (else just stop):
  - put(kind='memory', text=...)   a synthesis or inspiration note
  - link(...)                      connect related items
  - tag(...)                       label something (e.g. DREAM:speculative)
  - supersede(...)                 merge near-duplicate memories
  - acquire(identifier|title, ...) queue a missing paper to fetch

If in doubt, do nothing and stop. A quiet dream is a good dream.
```

No strict output JSON, no per-turn contract: writes happen as the agent
calls the write verbs; the run ends when it stops or hits a limit.

### Turn & cost limits

Turns can be **large-ish** \u2014 dreaming is a background job and we *want*
it to wander (read clusters, pull a ring, check an opposite, maybe one
external lookup). The real backstop is `--max-budget-usd` per run; turns
are a coarse safety net against pathological loops, not a tight leash.
Both default-off (whole feature gated by `PRECIS_DREAM_LLM`); knobs
`PRECIS_DREAM_MAX_TURNS`, `PRECIS_DREAM_MAX_USD`.

### Transcript capture

The full tool-call transcript (every search/get/put with args +
results) is written to a file **outside the repo tree**
(`PRECIS_DREAM_TRANSCRIPT_DIR`, default `~/.precis/dream-transcripts/`),
named by `attempt_id`. `dream_log.transcript_path` points at it. This
replaces the old per-call `verdict` JSON as the audit record: the
transcript *is* the reasoning trace, and `ref_events` still records the
concrete writes.

### Write surface & safety

- **Additive verbs** (`put`, `link`, `tag`) are unrestricted \u2014 a stray
  dream memory is inert clutter, trivially pruned by its `DREAM:` tag.
- **Consolidation is the one destructive behavior** and does *not* go
  through a raw `delete`. It routes through a dedicated **`supersede`**
  operation (delete + link-migration + `supersedes` edge + soft-delete,
  one tx \u2014 see \u00a7Consolidate behavior) so it stays atomic and
  reversible. The agent cannot hard-delete anything.
- Dreams write with a `DREAM:` tag (and `DREAM:speculative` for
  inspirations) so every agent-authored row is identifiable and
  fenced from authoritative search by default.

## Dream navigation tools (MCP surface)

The cleverness lives here, in a handful of read tools. **No new verbs**
\u2014 these are parameters on `search` and a new read-only `cluster` kind,
both reachable over MCP and in-process.

### Similarity knob on `search` (near / ring / opposite)

`search` grows three optional params, on top of the existing cross-kind
fan-out (`kind='*'`):

- `target` \u2014 desired cosine similarity to the seed (default `1.0` =
  today's nearest-first behavior).
- `tol` \u2014 half-width of the accepted band around `target`.
- `like=<ref|chunk id>` \u2014 seed by an existing item's stored vector
  instead of embedding a `q=` string (so a dream can pivot off a node).

Three regimes, with genuinely different mechanics:

- **`target \u2248 1` (adjacent).** The existing HNSW nearest-neighbor query.
- **`target = -1` (opposite pole).** The most anti-correlated item to
  `v` is the nearest neighbor of `-v`. Just negate the seed and run the
  ordinary ANN query (`ORDER BY embedding <=> (-v)`). One cheap query.
- **`target \u2248 0.5` / `-0.5` (a "ring").** Hard, because the set of points
  at a fixed intermediate cosine to `v` is not a point but a **cone** \u2014
  a whole `(d-2)`-sphere of directions ("many ways to get there"). Two
  affordable strategies, since dreaming is not latency-sensitive:
  1. **Cone sampling (preferred).** Pick a few random unit vectors
     `u_i \u22a5 v`; build anchors `w_i = cos\u03b8\u00b7v + sin\u03b8\u00b7u_i` (\u03b8 = acos(target));
     ANN near each `w_i`; keep hits whose *actual* cosine to `v` is in
     `[target\u2212tol, target+tol]`. K cheap ANN queries that naturally
     sample different slices of the ring \u2014 exactly the diversity we want.
  2. **Exact band scan (fallback).**
     `WHERE (1 - (emb <=> v)) BETWEEN lo AND hi ORDER BY random() LIMIT k`
     \u2014 O(N) but correct and simple; fine for a background job, and the
     reference implementation for testing the sampler.

> **Caveat (anisotropy).** Sentence-embedding spaces (BGE-M3 included)
> occupy a cone, not the full sphere \u2014 vectors are mostly positively
> correlated. True cosine \u2248 \u22121 essentially never exists; the "opposite
> pole" in practice is whatever is *least* similar (often cos \u2248 \u22120.1).
> A `target \u2248 0.5` ring is well-populated; negative targets return the
> sparse contrarian fringe. State this so nobody reads it as a bug.
>
> **Why not just page down?** Walking the nearest-first ranking until the
> band appears works for *high* targets (the band is near the top) but
> degrades badly for mid/low targets \u2014 in an anisotropic space cos\u22480.5
> can sit near the *median*, so you'd page through a huge fraction of the
> corpus, and HNSW quality falls off with deep pagination. Hence
> cone-sampling / band-scan for the interesting bands.

`view='ring'` and `view='opposite'` are convenience presets that set
`target`/`tol` defaults; raw floats remain available for precision.

### Cluster navigation: a read-only `cluster` kind

A new kind exposed through the existing verbs (no new verb):

- `get(kind='cluster')` \u2014 list the current salient clusters (numeric/
  file kinds already "list on no id"; `cluster` follows that contract).
- `get(kind='cluster', view='active')` \u2014 salience-ranked regions ("what
  mattered lately"), driven by the decayed `access_score` signal.
- `get(kind='cluster', view='unvisited')` \u2014 dense regions with **no**
  covering dream nearby (structure-driven; also the cold-start answer).
- `get(kind='cluster', id=N)` \u2014 one cluster's members + centroid +
  existing dream links, so the agent can drill in.

Clusters are computed by the **retrieve-then-cluster** machinery
(\u00a7Synthesize behavior): ANN-retrieve a salient frontier, cluster that
bounded subset. **Cluster ids are ephemeral by default** \u2014 computed per
call, not persisted \u2014 because the durable artifact is the dream + its
links, not a cluster table. (Open question: persist a refreshed
`clusters` table if stable ids across a run prove necessary for the
agent to reference a cluster it saw a few turns earlier; a cheap
alternative is to let the agent pin a cluster by its representative
member id via `like=`.)

### Where this sits in the seven-verb surface

No verbs added. `search` gains `target`/`tol`/`like`; `cluster` is a new
`supports_search` + list-on-`get` kind; `supersede` is a guarded
operation on the `memory` handler (not a new top-level verb \u2014 it is the
delete+migrate path of \u00a7Consolidate, exposed as a single tool so the
agent can't assemble it from raw `delete`). External providers are
already kinds. This keeps the surface migration intact.

## Consolidate behavior (memory-only)

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

### The `supersede` tool (how the agent consolidates)

The agent discovers near-duplicate memories with the navigation tools
(`search(kind='memory', like=<id>)` for adjacency), then calls **one
guarded operation** — it never assembles a merge from raw `delete`:

```
supersede(merge_ids=[...],   # >= 2 live memory ids
          new_text="...",    # the consolidated memory
          new_tags=[...])    # default: union of survivors' tags
```

What `supersede` does, **in one transaction** (so it's atomic and the
embedding cascade re-runs cleanly):

1. Create the new memory (`insert_ref` + `card_combined` chunk + merged
   tags: union of survivors minus control tags; the agent may prune).
2. **Migrate links** via new `Store.migrate_links(old, new)`: re-point
   every link touching any `merge_id` to `new` (preserve relation +
   other endpoint), `INSERT ... ON CONFLICT DO NOTHING` to dedup against
   the unique index, drop self-loops, then DELETE the old rows.
3. Add `new --supersedes--> old` per merged id (auto-mirrors).
4. Stamp `meta.superseded_by = new_id` on each old ref, then
   `soft_delete_ref(old)`. Originals stop surfacing; the survivor's link
   view shows them as "(deleted)".

**Guards (enforced by the tool, not the prompt):** all `merge_ids` must
be live `memory` refs (no papers — papers are never merged or deleted),
length ≥ 2, and the caller cannot target non-memory kinds. A bad call
fails with a typed error the agent can read and retry; it can never
corrupt or hard-delete.

**Audit.** Each `supersede` writes a `ref_events` row (source `'dream'`):
merged ids, link-migration counts, merged-into id — plus the run's
`dream_log` row + transcript capture the surrounding reasoning.

### Memories embeddable + supersede make this safe

- Memories gain `card_combined` chunks (above) so the agent's
  `search(like=...)` finds true semantic neighbours, not just title
  matches.
- Reversible at SQL (`deleted_at=NULL` + the `supersedes` edges).
- Because consolidation is *opt-in by the agent* and memories are
  low-value, the conservative bias is cultural (the prompt) plus the
  tool guards — not a rigid worker heuristic. If the agent is unsure, it
  simply doesn't call `supersede`.

### Soft-delete ↔ link semantics (checked against the code)

`supersede` soft-deletes the merged-away memories, so we verified what
that does to the graph (`store/_links_ops.py`, `store/_refs_ops.py`,
`handlers/_numeric_ref.py`). It is **safe and non-destructive**:

- **Non-cascading.** `soft_delete_ref` only sets `deleted_at=now()` on
  the ref; it never touches the `links` table. Link rows survive intact.
- **Links to a tombstone still resolve.** `links_for` joins
  `links → chunks` (for pos) but **never joins `refs`**, so it does not
  filter on the endpoint's `deleted_at` — a link whose other end is
  soft-deleted is still returned.
- **Today: rendered with a marker.** The links view currently fetches
  endpoints via `fetch_refs_by_ids(include_deleted=True)` and
  `_format_link_line` appends `" (deleted)"` when `ref.deleted_at` is
  set — so a link to a tombstone shows as `memory:<old> (deleted)`.
- **Fully reversible.** Because links are untouched, `deleted_at=NULL`
  on the old ref restores the original graph verbatim — this is what
  makes "reversible at SQL" actually true.

**Decision (general, not dreaming-specific): hide links to deleted
endpoints by default, on both sides; reveal only on explicit opt-in.**
A reference to a deleted thing is clutter — it shouldn't appear in the
normal links view in either direction (outbound *or* inbound). The
change is one predicate in the single read path, `Store.links_for`:

- Add `include_deleted_endpoints: bool = False`. LEFT JOIN `refs` on
  both endpoints and add
  `AND (include_deleted_endpoints OR (rs.deleted_at IS NULL AND rd.deleted_at IS NULL))`.
  Since the focal ref is alive, requiring both endpoints non-deleted
  reduces to "hide if the *other* end is deleted" — covering out, in,
  and the inverse-relation rewrite uniformly.
- Internal callers that must see every edge (the future `migrate_links`)
  pass `include_deleted_endpoints=True`.
- User-facing reveal: an explicit flag on the links view (surface TBD —
  e.g. `view='links'` + `show_deleted=true`, or a `view='history'`).

Consequence for `supersede`: after `migrate_links` *hard-DELETEs* the
old ref's edges (re-pointed to the survivor), the only remaining pointer
to the dead ref is the `new → supersedes → old` provenance edge — and
under the new default that edge is **hidden** unless the agent/human
explicitly asks for deleted endpoints. Provenance is preserved at SQL,
just not shown by default. No live ref dangles at a tombstone.

> **Must-verify guard (not a links issue).** Soft-delete does **not**
> remove the old memory's `card_combined` chunk or its
> `chunk_embeddings` row (chunks are append-only per AGENTS.md).
> Therefore `search` / `cluster` retrieval MUST filter on
> `refs.deleted_at IS NULL`, or a just-merged duplicate resurfaces as
> its own neighbour. Confirm `search_blocks_semantic` (and the cluster
> frontier query) apply this predicate; add a regression test.

## Synthesize behavior (cross-kind, non-destructive)

Generative RAPTOR-like layer. The agent reads any kind and writes only
`kind:dream`-tagged memories; never deletes. The **retrieve-then-cluster**
machinery below is what backs `get(kind='cluster', ...)`; the agent
triggers synthesis simply by reading a salient cluster and, if it's
worth a summary, calling `put(kind='memory', ...)` + `link(...)`.

### Retrieve-then-cluster (scales past 500k, powers the cluster tool)

Per cluster-tool call:
1. **Seed selection.** Pick salient seeds.
   - *Warm:* high-churn regions (top chunks by decayed `access_score`).
   - *Idle:* dense, **undreamt** clusters (structure-driven) — regions
     with many chunks and no `derived-from`-linked dream within ε of the
     centroid. This is also the **cold-start** answer: dreaming
     bootstraps from usage; when quiet, it pre-chews dense uncovered
     regions.
2. **Frontier retrieval (HNSW, scales).** For each seed, ANN top-k →
   union into a working set of a few thousand chunks. Never loads the
   full matrix.
3. **Cluster the working set (cheap).** HDBSCAN/GMM (soft assignment)
   over the few-thousand-vector subset — genuine structure, sub-second,
   memory-bounded regardless of corpus size. *(Adds `scikit-learn` /
   `umap` — ADR-gated cross-package threshold, taken when the cluster
   tool is built.)*
4. **Surprise is now a tool, not an env knob.** The old worker
   `PRECIS_DREAM_WILDCARDS` is gone — in the agentic model the agent
   pulls a far-away or *opposite* member itself via the similarity knob
   (`search(like=<centroid member>, target=-1 | view='ring')`) when it
   wants grounded surprise (this is the Inspire behavior).
5. **Synthesis is the agent's write.** When a cluster is worth a
   summary, the agent writes a dream-origin memory with
   `put(kind='memory', ...)`, links it to its sources, and adds temporal
   keywords (ISO week/month) so dreams are time-addressable.

> **Relation decision (settled):** reuse the existing
> `derived-from` / `derived-into` pair for dream→source provenance. We
> do **not** add a `summarises` relation — the distinction hasn't earned
> its keep, and reusing an existing relation avoids a migration. All
> references below use `derived-from`.

### Index = the link graph (no tree table)

Don't persist cluster tables. The durable artifact is dream-origin
memories + their `derived-from` links; clustering output is thrown away
each call. Retrieval is plain pgvector search over chunks (dream cards
included) → collapsed-tree retrieval for free, scales with HNSW.

### Recursion (bottom-up, gets cheaper with height)

Dream memories are themselves embeddable memories, so a later pass
clusters *them* too. Level 0 (500k+) → frontier-only; level 1 (dream
memories, hundreds–thousands) → can cluster more globally; level 2+
→ tiny, fully global. Track height with `meta.dream_level`; children
via `derived-from`. Cap depth + cooldown to avoid runaway.

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
  dreams sink rather than mislead. Always expose `derived-from` links so
  the agent can drill to primary sources (no dead ends; survives cold
  start). **Inspirations are exempt from this boost** — see Inspire.

## Navigate / TOC behavior (a shape of synthesize)

Not a separate behavior so much as a *shape* of the synthesis write:
instead of a prose summary, the agent emits a **navigable outline**
memory over a region — reusing precis's existing TOC vocabulary
(`segment_toc` worker, `ref_segments`, `views=("toc", …)` on handlers),
but at cross-ref / region scope instead of per-ref.

- **Output:** a memory whose body is an outline; each entry =
  `<subcluster gloss>` + `derived-from` links to representative source
  chunks/refs ("expound with context" = gloss + drill-down, not a wall
  of prose).
- **Guards:** cap entry count; only expound subclusters above a
  size/salience floor so a TOC doesn't balloon.
- **Shares everything else** with synthesis: the cluster tool, the
  recursion (`meta.dream_level`), relative-recency cooldown, search
  boost. The agent just chooses an outline-shaped `put` when a region is
  better navigated than summarized.

## Inspire behavior (speculative, fenced)

The creative-recombination behavior — and the reason the similarity knob
exists. The agent:

- Picks an **active cluster** (`get(kind='cluster', view='active')` =
  "what we're worried about now").
- Pulls **remote/opposite stimuli** with the knob itself:
  `search(like=<cluster member>, view='ring')` for moderately-distant
  ideas or `target=-1` for the opposite pole — cross-kind
  (`kind='*'`) so any kind can fertilize.
- Judges, in its own head, whether there's a *realistic* way to apply a
  stimulus to the issue. Bias: say no. Most reads → nothing, and that's
  fine.
- **On a real hit:** `put(kind='memory', ...)` a low-confidence note
  tagged `kind:inspiration` + `DREAM:speculative`, then `link` it to
  *both* parents (issue cluster + stimulus) so the provenance — "this
  idea came from applying X to Y" — is traceable.

**Fencing (decided):** inspirations are NOT boosted in default search
and do not pollute authoritative results. They surface on explicit ask
or a dedicated view, and the `DREAM:speculative` tag makes pruning
trivial. Later Mode-1 consolidation or a human can promote the good
ones.

**Autonomy:** autonomous-write is acceptable *because* it's fenced and
non-destructive — a bad inspiration is inert clutter, never a
corruption. Cost is the main risk: tightest per-call budget, lowest
frequency, default-off.

## Acquire behavior (surface & fetch missing papers)

A dream is well-placed to notice the corpus is *missing* a paper it
keeps bumping into. The decisive fact: **the fetch pipeline already
exists end-to-end** (see §grounding) — so the agent only mints a stub,
and everything downstream is automatic.

### The `acquire` tool (guarded, like `supersede`)

```
acquire(identifier=<doi|arxiv|s2> | title=...,   # what to get
        reason="...",                            # why it's worth holding
        context_ref_id=<ref>)                    # where it came up
```

It does the minimum, then gets out of the way:

1. Resolves metadata via S2 (`get_paper_by_id` / `lookup_s2`) — enough
   to mint a meaningful stub (title, year, ids).
2. Upserts a **stub paper ref** *idempotently* (identifier-collapse via
   `ref_identifiers`: a hit on an already-held or already-wanted paper
   short-circuits to a no-op), with `meta.set_by='dream'` and a
   `DREAM:acquire` tag — reusing the exact path `chase` already uses
   (`_resolve_or_create_stub`).
3. Links the stub to `context_ref_id` (provenance) and to the dream.
4. Returns immediately. **It never ingests inline** — no Marker, no
   download, in the dream turn.

### Auto-fetch vs the required-papers list (the user's rule, for free)

The stub the tool mints is identical to a chase stub, so the existing
machinery applies with zero new wiring:

- **Auto-acquirable (OA exists):** the `fetch_oa` worker auto-claims the
  stub on a later pass, finds an OA PDF (Unpaywall → arXiv → S2), and
  `precis add` promotes it to a full paper (body chunks + embeddings).
  The agent did nothing special.
- **Not auto-acquirable (paywalled / no OA):** `fetch_oa` logs
  `no_oa_version` and the stub **stays in the backlog** that
  `precis stubs` renders — *the required-papers list*. New wants append
  in stub-creation order; a human (or the `doilist` operator flow)
  drains it, and if an OA copy later appears `fetch_oa` grabs it
  automatically.

### Gating & safety

Behind its own `PRECIS_DREAM_ACQUIRE` gate. Minting a stub is additive
and reversible (soft-delete), and because heavy ingest runs in the
normal worker rotation, a runaway dream can at worst enqueue paper
stubs — it can never blow the run budget on downloads or ingest. The
`acquire` tool, like `supersede`, is the *only* way the agent touches
the paper-acquisition path; it cannot drive `fetch_oa` or `precis add`
directly.

## External search (dreams reaching outward)

Dreaming need not be closed-corpus. A dream can pull in *new* external
knowledge — to ground a speculative inspiration (has someone already
done this? what's the prior art?) or to fetch a reference a cluster
keeps citing but we don't hold.

**No special plumbing — they're already MCP kinds.** Semantic Scholar
and Perplexity are existing precis kinds (`websearch` / `think` /
`research`, plus the S2 ingest path), each cache-backed: a call parses,
embeds, and persists a ref + `cache_state` row, so results are
**immediately searchable and linkable** and re-asking is free. In the
agentic model the dream just *calls them like any other tool* —
`get(kind='websearch', q=...)` — and the caching, dedup, and cost
tracking come for free from the handler. (This is why agentic-over-MCP
doesn't sacrifice cache discipline: the cache lives in the handler,
hit via `runtime.dispatch` regardless of caller.)

**Cost is a hint, bounded by the run.** The prompt nudges toward the
cheap tiers — free Semantic Scholar and ~$0.001 `websearch` first,
`think` when it helps, deep `research` only when clearly worth it — but
there's no per-search ceiling; the only hard cap is the run's overall
`--max-budget-usd`. Gated behind `PRECIS_DREAM_SEARCH` (separate from
the `PRECIS_DREAM_LLM` master gate) so external reach can be disabled
without disabling dreaming. Warrants its own ADR.

## Deterministic fallback — prompts & output contracts (OPTIONAL)

> **Not the primary path.** The sections above (agent loop + navigation
> tools) are the design. This section is retained as an **optional
> deterministic fallback**: a worker-mediated, single-shot-per-call
> implementation of each behavior with strict JSON contracts, for use
> if the agentic loop proves too costly/flaky or for cheap regression
> testing of the underlying writes. It does *not* use MCP tool-calling —
> it's the old `call_claude_p` propose/dispose model. Skip it on a first
> read.

All fallback LLM calls go through the same `call_claude_p(prompt,
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

One row per **agentic run** (not per micro-decision — the agent's
internal exploration lives in the transcript). It records what a run
did, what it cost, and where the full trace is. Together with the saved
transcript it's the substrate for "optimize the dreaming".

```sql
CREATE TABLE dream_log (
  attempt_id       BIGSERIAL PRIMARY KEY,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  outcome          TEXT NOT NULL,        -- wrote | noop | error
  behaviors        TEXT[],               -- consolidate|synthesize|toc|inspire|acquire (what it did)
  seed_clusters    JSONB,                -- region(s) it started from (representative member ids)
  result_ref_ids   BIGINT[],             -- refs created/affected this run (empty on noop)
  turns            INTEGER,              -- agent turns used
  tool_calls       INTEGER,              -- total MCP calls
  model            TEXT,
  cost_usd         DOUBLE PRECISION,
  transcript_path  TEXT,                 -- file holding the full tool-call trace
  summary          JSONB                 -- agent's closing note + counts
);
```

- **Keep everything forever.** No pruning, no retention window. `noop`
  runs are kept too — "the agent looked at region X and left it alone"
  is exactly the signal we want for tuning. Nothing here surfaces in
  normal search; it's analysis-only.
- **The transcript is the rich record.** Fine-grained provenance —
  *which* remote/opposite stimulus led to an inspiration — comes from
  (a) the `derived-from` links on the created inspiration (both parents
  linked) and (b) the saved transcript, not from a structured
  `stimulus_dist` array. This is the cost of going agentic: telemetry
  moves from typed columns to the trace.
- **Analytic dimensions** still queryable cheaply: fruitfulness
  (`wrote / total`), behavior mix (`behaviors`), cost/turns per run.
  Deeper "what fertilizes what" analysis parses transcripts offline.
- **Volume is fine:** dreaming is gated, default-off, low-frequency, so
  one row + one transcript file per run is negligible.
- Indexes: `(outcome, created_at)`; optional GIN on `behaviors`.

## CLI wiring

A single `--only dream` pass in `cli/worker.py`, default-off in the
normal rotation (like `fetch_oa`). There is **no `--dream-mode` switch**
any more — one agent chooses its own behavior. The pass launches the
`claude` binary with the precis MCP config, `--max-turns`, and
`--max-budget-usd`, then records the run. Env knobs:
`PRECIS_DREAM_LLM` (master gate), `PRECIS_DREAM_MODEL` (opus-class),
`PRECIS_DREAM_MAX_TURNS`, `PRECIS_DREAM_MAX_USD`,
`PRECIS_DREAM_SEARCH` (external-reach gate),
`PRECIS_DREAM_ACQUIRE` (paper-stub-minting gate),
`PRECIS_DREAM_TRANSCRIPT_DIR`, the two salience half-lives
(`PRECIS_DREAM_HALFLIFE_ACCESS_DAYS`, `PRECIS_DREAM_HALFLIFE_DREAM_DAYS`
— see §Salience for `β`/`τ`/weights), plus cluster-size + staleness
thresholds. (`PRECIS_DREAM_WILDCARDS` /
`STIMULUS_MODE` / `MAX_DIST` are retired — surprise is now the
similarity knob, neighbour distance a `tol` arg.)

## Test plan

Most behavior is now the agent's, so tests target the **tools** (which
are deterministic and in-process testable) and the **run harness**, not
LLM judgment.

- **Salience:** lazy-decay math (`λ_access` half-life, floor-to-zero),
  in-place update, fire-and-forget best-effort write (read-side no-write,
  off the response path);
  migration backfills `last_seen=created_at` + seeded `access_score`
  (both `NOT NULL`, so the read path has no NULL branch); ingest seeds
  `A` source-weighted; **dream-actor reads accrue weight 0** (feedback
  guard); ref-level rollup = mean + mass bonus (identity for a 1-chunk
  memory).
- **Target-worthiness / cooldown:** `worth = Â·prior·(1−β·C)`; `β=1`
  fully suppresses a covered+unchanged region while `change_gate`
  reopens it once new members land after the dream; two-tier `C`
  (synthesized vs examined-noop from `dream_log.seed_clusters`) with
  `λ_dream` expiry; **repeated no-ops on a region escalate its cooldown
  (backoff) so a persistently-barren-but-salient region is exiled, and
  a change resets the count**; seed selection samples (softmax `τ`), not
  argmax.
- **Similarity knob:** `target=-1` returns the nearest neighbour of
  `-v` (exact, vs brute-force); cone-sampling returns members whose
  *actual* cosine to `v` lands in `[target±tol]` (vs the exact
  band-scan reference); `like=<id>` seeds from the stored vector;
  `target~1` is byte-identical to today's nearest search;
  `view='ring'`/`'opposite'` presets resolve to the right `target/tol`.
- **Cluster kind:** `get(kind='cluster')` lists; `view='active'`
  ranks by decayed `access_score`; `view='unvisited'` excludes regions
  with a `derived-from` dream near the centroid; `get(...,id=N)`
  returns members + centroid + dream links; cross-kind fan-out
  (`kind='*'`) merges via RRF (existing behavior, regression-guarded).
- **`supersede` tool:** `Store.migrate_links` (re-point,
  dedup-on-collision, self-loop drop, in+out, chunk-pos); card-chunk
  emission + re-embed; survivor surfaces / originals soft-deleted with
  `supersedes` edges + `meta.superseded_by`; **guards** reject
  non-memory ids, <2 ids, and dead ids; `ref_events` shape.
- **Soft-delete ↔ links (hide-by-default):** `links_for` omits links to
  a deleted endpoint on **both** sides (outbound + inbound + inverse
  rewrite) by default, and returns them when
  `include_deleted_endpoints=True`; the user-facing links view hides
  them unless the reveal flag is set. After `supersede`, the survivor's
  default links view shows **no** dead endpoint (the `supersedes` edge
  is hidden until asked for); a superseded memory is **absent** from
  `search(kind='memory')` and cluster member lists (deleted-ref
  exclusion — catches a missing `deleted_at IS NULL` predicate);
  `deleted_at=NULL` restores the original (visible) link graph verbatim.
- **Agent harness:** mocked `claude` (`PRECIS_CLAUDE_BIN` stub) that
  emits a scripted tool-call sequence → assert writes happened; a
  **no-op run** (agent reads then stops) writes a `dream_log` row with
  `outcome='noop'`, empty `result_ref_ids`, and a transcript file;
  `--max-turns` / `--max-budget-usd` truncation recorded; transcript
  written under `PRECIS_DREAM_TRANSCRIPT_DIR`.
- **Search boost / fencing:** dream-origin memories ride fused search
  with the relative-recency bump; `DREAM:speculative` inspirations are
  absent from the default boost; `derived-from` drill-down works.
- **`acquire` tool:** mints a `pdf_sha256 IS NULL` stub tagged
  `DREAM:acquire` with `meta.set_by='dream'`; idempotent on a known
  identifier (re-acquire = no-op); links to context; the stub is
  claimable by `claim_stubs_to_fetch` (regression: a dream stub and a
  chase stub are indistinguishable to `fetch_oa`); never ingests inline.
- **Deterministic fallback (optional path):** the legacy JSON contracts
  — `ClaudePError` → `noop`; schema-invalid → `noop`; id-handle
  rejection — only if the fallback is built.

## Definition of done (per AGENTS.md)

- Plan reviewed (this doc).
- ADRs in `docs/decisions/`: (a) `supersedes` relation + the guarded
  `supersede` tool (additive-only agent writes, no raw delete);
  (b) salience model + columns on `chunks` + the `dream_log` table;
  (c) similarity knob on `search` (`target`/`tol`/`like`, cone-sampling)
  + the read-only `cluster` kind, incl. the clustering dependency
  (`scikit-learn`/`umap`) — taken when the cluster tool is built;
  (d) the **agentic dream loop** (claude-over-MCP, turn/cost-bounded,
  transcript capture, default-off) — the headline capability ADR;
  (e) external-reach gate (`PRECIS_DREAM_SEARCH`); (f) acquire behavior
  — the guarded `acquire` stub-minting tool + `PRECIS_DREAM_ACQUIRE`,
  reusing the chase-stub / `fetch_oa` loop.
- `0003_dreaming.sql` applies cleanly to a fresh DB (salience columns,
  `supersedes` relation, `dream_log` table); only the new file pending
  under `precis migrate --dry-run`.
- Navigation tools shipped + tested in-process (similarity knob,
  `cluster` kind, `supersede`); `--only dream` has `--help`, an
  integration test (mocked `claude`), and a README line.
- The agent loop runs end-to-end against the MCP server with a real
  (gated) model at least once; a no-op run and a writing run both
  produce correct `dream_log` rows + transcripts.
- Full check green (`ruff check`, `ruff format --check`, `mypy`,
  `pytest`); version bump + `CHANGELOG` entry.

## Suggested sequencing

Tools first (deterministic, independently useful), then the agent on
top.

1. Salience columns + lazy decay + batched flush (useful on its own for
   search ranking).
2. Memory `card_combined` chunks + backfill + `supersedes` migration +
   `dream_log` table.
3. Similarity knob on `search` (`target`/`tol`/`like`: `-v` opposite +
   cone-sampling, band-scan reference). Useful to any caller, not just
   dreams.
4. `cluster` kind (retrieve-then-cluster behind `get` views) + the
   `supersede` tool. Behind the clustering-deps ADR.
5. Search boost + fencing for the dream/memory layer.
6. **The agent loop** (claude-over-MCP, `--max-turns`/budget, transcript
   capture, `dream_log`) behind `PRECIS_DREAM_LLM`. This is where the
   behaviors come alive.
7. External reach: flip `PRECIS_DREAM_SEARCH` (providers are already
   kinds) + its ADR.
8. Acquire: the `acquire` stub-minting tool + `PRECIS_DREAM_ACQUIRE`
   (the `fetch_oa` / `precis stubs` machinery already exists, so this is
   just the guarded tool + a gate).
9. *(Optional)* deterministic fallback worker, if the agent loop proves
   too costly/flaky.

## Open questions for the reviewer

1. Access-event weights (cite / read / search-impression) — starting
   values?
2. `cluster` ids: ephemeral (computed per call) vs a persisted,
   refreshed `clusters` table — needed only if the agent must reference
   a cluster it saw several turns earlier (the `like=<member>` pin is a
   cheaper alternative). Start ephemeral?
3. `--max-turns` default (large-ish) and per-run `--max-budget-usd`
   default — pick starting values; budget is the real backstop.
4. Transcript format + retention: full session JSON vs a trimmed
   tool-call log; keep forever or age out old transcripts?
5. Half-life, cluster-size, staleness, ring `tol` defaults — pick
   conservative values and tune from `dream_log` telemetry.

_Resolved:_
- **Full-agentic** loop (claude-over-MCP, turn/cost-bounded) is the
  design; per-mode workers are an optional deterministic fallback.
- **No action is a first-class outcome**; memories are low-value and
  reversible, so trust the model and bias toward doing nothing.
- **Writes are additive** (`put`/`link`/`tag`); destructive merges go
  only through the guarded `supersede` tool (no raw delete).
- **Similarity knob** (near/ring/opposite) is feasible: `-v` for the
  pole, cone-sampling for the ring; anisotropy means true `-1` is rare.
- **Search spans all kinds** via the existing `kind='*'` fan-out.
- **Provenance relation** = reuse `derived-from` (no new `summarises`).
- **Transcript to file**; `dream_log` is one row per run + a pointer.
- External reach: providers are already kinds; cost is a prompt hint
  bounded by the run budget; gated by `PRECIS_DREAM_SEARCH`. (Patents
  deferred — kind exists but currently unavailable.)
- **Acquire = auto-fetch, gated.** The dream mints a paper stub; if an
  OA copy exists the existing `fetch_oa` worker auto-ingests it, else it
  appends to the `precis stubs` required-papers backlog. No inline
  ingest; gated by `PRECIS_DREAM_ACQUIRE`.
- `dream_log` retention — keep everything forever (incl. no-ops),
  analysis-only.
