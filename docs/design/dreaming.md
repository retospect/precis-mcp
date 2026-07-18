# Dreaming -- agentic memory consolidation + synthesis

Status: **proposed** (plan-first artifact; no code landed yet)
Author: (fill in)
Date: 2026-06-05 (agentic re-architecture 2026-06-06)

> **Architecture note (2026-06-06).** Dreaming is now a **full-agentic**
> loop: a thin worker hands an opus-class model a turn/cost budget and a
> connection to precis's own MCP tools, and lets it roam. The behaviors
> below are no longer separate workers -- they are *behaviors*
> the one agent may choose. The intelligence lives in the **navigation
> tools** (§Dream navigation tools) and the agent loop (§The dreaming
> agent), not in worker control flow.

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
   been worried about lately"* -- and no signal for what *recently
   mattered* to focus such synthesis on.

**Dreaming** is a single **agent**, not four workers. A thin worker
launches an opus-class model connected to precis's MCP surface, gives
it a turn/cost budget, and one instruction: *roam the knowledge base
and improve it, leaving at least one small change.* The taxonomy below is principled -- it
mirrors the leading hypothesis for what sleep does: *consolidation* and
creative *recombination* -- but each is now a **behavior the agent may
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
  backlog. The dream only mints the stub -- it never ingests inline.

**Guiding philosophy (decided).** Memories are *low-value and
reversible*, and the model is clever -- so **trust it, but make it
leave a mark.** In this evaluation phase every run must commit **at
least one small change** -- a new note, a link, or a conservative
merge -- so we can inspect the output and judge quality directly.
Everything is `DREAM:`-tagged and reversible, so a weak dream is cheap
to ignore or undo. Bias toward *small*, not toward *nothing*; once
quality is calibrated we may re-allow silent no-op runs.

Shared infrastructure under the agent: a **salience signal** on chunks
(what mattered lately) powering the cluster-navigation tools; a
**`dream_log`** recording every run + a saved transcript for the
feedback loop; **additive or guarded-merge writes** through the normal
MCP verbs so a bad dream is inert clutter, never corruption.

## Decisions (settled in discussion)

- **Consolidation neighbours are semantic.** Memories get a
  `card_combined` chunk + embedding so `search(like=...)` uses real
  cosine similarity, catching paraphrases a lexical title match misses.
- **Link model unchanged; memory = ref + 1 card chunk.** A memory is a
  `ref` with a single `card_combined` chunk (`ord=-1`) for its body.
  Links stay **ref-anchored** (ref→ref, with `chunk_id` an optional
  *body* locus only -- never a card, since cards are re-emitted via
  DELETE+INSERT). So a memory's links are keyed on its stable `ref_id`
  and always surface, regardless of card churn. No chunk→chunk rewrite,
  no migration. (Soft-delete is ref-only -- `chunks` has no `deleted_at`.)
- **Memory-only consolidation; papers are never merged or deleted.**
  Synthesis *reads* papers as cluster inputs but only ever *writes* new
  memories.
- **Full-agentic, turn/cost-bounded.** No worker-coded mode dispatch and
  no per-call JSON contract on the hot path: a thin worker launches an
  opus-class model connected to the precis MCP server, capped by
  `--max-turns` (large-ish is fine -- dreaming isn't latency-sensitive)
  and `--max-budget-usd`, gated default-off behind `PRECIS_DREAM_LLM`.
  The agent reads, decides, and writes through ordinary MCP verbs.
- **The navigation tools are the product, not the prompt.** The cleverness
  lives in two `search` modes -- `view='dreamable'` (the focus region) and
  the `angle`/`n` spray (distinct sparks) -- see §Dream navigation
  tools. The prompt is deliberately minimal: *pick something and make
  one small improvement.*
- **Writes are additive or a guarded merge; each run leaves >=1 small
  change.** The agent may `put` new memories and `link` / `tag`
  existing ones; destructive consolidation (delete + link-migration)
  routes through a guarded `supersedes` path, never a raw `delete`.
  Soft-delete + the `supersedes` chain keep every merge reversible at
  SQL. In the eval phase a no-op is *not* the goal -- commit at least
  one small change so we can inspect quality.
- **Every run leaves a transcript, in the DB.** The full agent tool-call
  transcript lands in a `dream_transcripts` row (1:1 with `dream_log` via
  `attempt_id`); `dream_log` itself stays lean for analytics. One store,
  transactional, no orphaned files. That is the audit trail (alongside
  `ref_events` for the writes themselves).
- **Target selection = date-only, knob-free.** The dream seed is
  `argmax(last_seen - last_dreamt)` over `paper`+`memory` chunks -- two
  `NOT NULL` timestamps, no decay, no coefficients, no sampling.
  Surfacing resets `last_dreamt`, so the corpus rotates on its own and a
  dreamt region drops out deterministically (no `worth`/`β`/softmax, no
  separate cooldown). `accesses` is kept for heatmaps only. See
  §Target selection.
- **Focus region = retrieve-then-cluster.** `search(view='dreamable')`
  ANN-retrieves a salient frontier (HNSW), then runs real clustering
  (HDBSCAN/GMM) on that bounded subset -- never global GMM over the
  whole corpus (>500k chunks and growing). Clustering deps land behind
  an ADR when it's built; there is no `cluster` kind.
- **`new_tags` default = union of survivors' tags** (minus control
  tags); the LLM may prune.
- **Dream log keeps everything forever.** One row per run, retained
  including `noop` runs -- no pruning, no retention window. Nothing in it
  surfaces in search or any normal view; it's analysis-only. The
  retained log + transcripts are the dataset for "optimize the
  dreaming": fruitfulness rate, behavior mix, which regions fertilize.
- **Search spans all kinds by default.** `search` already supports
  cross-kind fan-out (`kind='*'` / `'all'` / `'paper,memory'`,
  RRF-fused) -- the agent uses it directly. `get` stays single-kind
  (it's id-addressed).
- **One similarity knob: `angle` + `n`.** `search` grows `angle`
  (target cosine, default 1), `n` (how many distinct items), and a
  `like=<id>` seed, so the agent can ask for *adjacent* (`angle=1`), a
  *cone* of moderately-related ideas (`angle=0.5`), or the *opposite
  pole* (`angle=-1`). See
  §Dream navigation tools for the (feasible) mechanics.
- **Dreams may reach outward.** Semantic Scholar and Perplexity are
  already MCP kinds (`websearch` / `think` / `research`, cached), so an
  agentic dream simply calls them like any other tool -- no special
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
  -- candidate retrieval scales to millions.
- `chunks` already has a `meta JSONB` column; chunk text is
  append-only with an embedding/summary cascade. **Non-text columns do
  NOT trigger that cascade** -- so a salience column is safe to UPDATE
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
  **`supersedes` / `superseded-by` now exist** (migration 0007 +
  `Relation` Literal); the guarded `supersede` tool is implemented on
  `MemoryHandler` (`Store.migrate_links` + `soft_delete_ref(conn=)` +
  `stamp_ref_meta`).
- Tags: `tags(namespace, value)` is generic data (no migration for new
  values). **Decided + implemented:** `DREAM:` is a *closed* axis
  (`_CLOSED_VOCAB["DREAM"] = {consolidated, speculative}`, added to
  `_KIND_ALLOWED_AXES["memory"]`). An open tag was considered but the
  tag parser routes every `UPPERCASE:` prefix to the closed namespace,
  so an agent writing `DREAM:speculative` through the validated
  `tag`/`put` verb needs the axis registered to pass `parse_strict` —
  and the closed vocab gives typo protection for free. Two app-side
  lines, no SQL. (`DREAM:acquire` for the gated stub tag is deferred to
  the acquire tool; it also needs `DREAM` on `paper`'s allowed axes.)
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
  surface -- the agent uses it as-is.
- **MCP is a thin shell over `runtime.dispatch`.** Every MCP verb (and
  every resource read) routes through `runtime.dispatch(verb, args)`
  (`mcp_modalities._read_resource`), which hits the same handlers a
  worker can call in-process. New navigation tools therefore land as
  ordinary handler verbs/views and are reachable both over MCP (for the
  agent) and in-process (for tests).
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
    (`cost_per_call_usd` in `perplexity.py`), not a measured price --
    verify against current Perplexity pricing if it matters; the cheap
    tiers are the default anyway.
  - Cache discipline: `cache_state` (provider, request_hash, TTL,
    cost) + `CACHE:`/`WATCH:` tags give dedup, idempotency, and cost
    tracking for free (`store/_cache_ops.py`).
- **Acquire pipeline already exists end-to-end.** The chase worker
  mints **stub paper refs** when a citation resolves to a paper we don't
  hold (`chase._resolve_or_create_stub`: probe `ref_identifiers`, else
  `insert_ref(kind='paper', meta={set_by})` + register every id --
  idempotent via identifier-collapse). A stub is a `paper` ref with
  `pdf_sha256 IS NULL`. The **`fetch_oa` worker**
  (`claim_stubs_to_fetch`) then auto-claims *any* such stub carrying a
  DOI/arXiv/S2 id and cascades Unpaywall → arXiv → S2; on `fetch_ok` the
  PDF lands in the watch inbox → `precis add` promotes the stub to a
  full paper. No OA copy → `no_oa_version`, the stub stays in the
  backlog that `precis stubs` renders. So "auto-fetch if possible, else
  a required-papers list" already exists -- a dream just has to mint the
  stub.
- Worker scaffold: `run_finding_chase_pass` -- claim
  `FOR UPDATE ... SKIP LOCKED`, one ref per tx, default-off LLM, write
  `ref_events`, mutate `meta`, re-emit `card_combined` via
  DELETE+INSERT, flip a tag. CLI registers ref-passes in
  `cli/worker.py` (see `fetch_oa` for a default-off pass).

## Target selection (date-only, knob-free)

The seed for a dream is chosen by **one ranking key over chunks** -- no
decay, no coefficients, no sampling. Two timestamps and a subtraction.

### Storage -- two timestamps (+ a counter) on `chunks` (migration)

These are **metadata-only** columns: no content, no embedding/summary
cascade -- so mutating them does **not** breach the "chunks body is
append-only" invariant (which guards `chunks.text`). Paper/memory
*content* stays sacrosanct; only these salience fields move.

```sql
ALTER TABLE chunks
  ADD COLUMN last_seen   TIMESTAMPTZ NOT NULL DEFAULT now(),  -- last EXTERNAL access
  ADD COLUMN last_dreamt TIMESTAMPTZ NOT NULL DEFAULT now(),  -- last surfaced by a dream
  ADD COLUMN accesses    INTEGER     NOT NULL DEFAULT 0;       -- analytics only (heatmaps)

-- everything starts at its birth: neither hot nor suppressed
UPDATE chunks SET last_seen = created_at, last_dreamt = created_at;

-- one set-based, in-DB bump: a single round-trip for a whole result page
CREATE FUNCTION bump_salience(ids bigint[]) RETURNS void
LANGUAGE sql AS $$
  UPDATE chunks SET last_seen = now(), accesses = accesses + 1
  WHERE chunk_id = ANY(ids);
$$;
```

All `NOT NULL`, init = `created_at` → **no NULL guard**. `accesses` is
kept for heatmaps / observability **only** -- it does *not* enter the
ranking (the dates suffice).

### The score (one line, no knobs)

```
score(chunk) = last_seen - last_dreamt        -- a duration; argmax wins
```

Pick `argmax`. That is the whole model.

- **It always picks something -- we're never bored.** No candidate
  filter, no no-op-on-empty. Even when *everything* has been dreamt, the
  top item is the one **least-recently-dreamt relative to its last
  access** -- a sensible "most due" pick, not random. (This corrects the
  earlier "empty set → do nothing".)
- **Dreaming resets the score → no softmax needed.** Surfacing sets
  `last_dreamt = now` on every returned chunk, which deterministically
  drops the winner to the bottom; next run a *different* region tops, so
  the corpus **rotates on its own**. The act of looking *is* the
  anti-repeat mechanism (no stochastic spreading, no `dreamt` counter).
- **New / edited material.** Ingest/edit init `last_seen = last_dreamt =
  created_at` → score 0: neither hot nor suppressed. It enters the
  rotation during lulls and jumps up the moment something accesses it.
  (We accept that brand-new material may be dreamt during a quiet period
  even before access -- consistent with "never bored".)

### Access accounting (the only write)

On each **external** access: `last_seen = now()`, `accesses += 1`.

- **Dream-actor reads excluded** (filtered on `set_by`) -- otherwise the
  dreamer heats its own wandering into an echo chamber. (Later human
  access to a dream-written memory *does* count -- the fruitfulness
  signal.)
- **One in-DB call on the search path.** The bump is a single
  `bump_salience(ids)` for the whole result page -- set-based, one
  round-trip, cheap. `thresholds.md` is **relaxed to allow
  metadata-only writes here**: its rule exists to protect *content*
  (paper/chunk text is immutable), which this never touches. Still
  lossy-tolerant, so it may be fired async under contention -- a missed
  bump barely moves a date.

### Selection algorithm (the worker)

1. `argmax(last_seen - last_dreamt)` over **target-kind** chunks (§Scope)
   → the **seed chunk**.
2. ANN around the seed → the **focus region** (this *is*
   `search(view='dreamable')`, §Dream navigation tools).
3. **Inject always**: the focus region **+ a few inspiration sparks**
   (the `angle` spray) into the prompt.
4. Let the LLM work.
5. At run end, set `last_dreamt = now()` on **every chunk the run
   touched** -- focus region, sparks, and anything the agent pulled via
   `search`/`get`, server-side from the session's tool-call log. If the
   dream looked at it, it has been dreamt; a later external access
   revives its score.

### Scope -- which kinds are targets vs sparks

Not every kind should be dreamt *on*, but more kinds can *fertilize*:

- **Dream targets (the `argmax` pool):** **`paper` + `memory` +
  `draft`**. These are durable, agent-writable knowledge where synthesis
  / link / supersede have something to do. `draft` is the project
  write-up we are *actively* building — the live prose we think about
  most, exactly where a wandering re-read pays off (spot a gap, a
  contradiction with a cited paper, a paragraph that drifted). Retired
  (soft-deleted) draft chunks are excluded from the pool. (Dream-authored
  memories are themselves `memory`, so they re-enter the pool; runaway
  volume is bounded by pruning low-value dreams (see Recursion).)
- **Inspiration sparks (the `angle` spray):** **`paper` + `memory` +
  `draft` + `oracle`**. `oracle` is read-only curated wisdom (Stoic principles,
  engineering rules of thumb) -- useless as a *target* (no `put`, not
  ours to consolidate) but **excellent fertilizer** at an angle from a
  research cluster. Precondition: the kind must be in the embedding/ANN
  index (oracle entries are already `search_hits`-able).
- **Excluded entirely:** **`skill`** (tooling how-to docs -- dreaming on
  "how to use precis" is noise), and transient/cache kinds
  (`websearch` / `think` / `research`, file kinds, `todo`/`gripe`).
  Widen later behind an ADR if a kind earns its place.

## The dreaming agent (loop, prompt, budget)

> **Implemented — the `claude` binary via `call_claude_agent`
> (ADR 0024).** The in-process litellm experiment was reversed: the pass
> runs `claude -p` (SOUL.md persona + packaged `data/prompts/dream-prompt.md`
> + MCP precis config, WebFetch/WebSearch off) through
> `src/precis/workers/dream_agent.py`, sharing the structural/deep
> reviewers' cost/timeout/turn caps. Step 2 below (launch the `claude`
> binary) is therefore the live shape. Gated by `PRECIS_DREAM_AGENT=1`
> and explicit `--only dream_agent`.

The worker is **thin**. Per scheduled run it:

1. Checks the gate (`PRECIS_DREAM_LLM`). No separate cooldown -- surfacing
   a region resets its chunks' `last_dreamt`, so its score drops and a
   different region tops next run (see §Target selection).
2. Launches the `claude` binary connected to the precis MCP server,
   with `model=$PRECIS_DREAM_MODEL` (opus-class), `--max-turns`
   (large-ish; see below) and `--max-budget-usd`.
3. Feeds the **minimal prompt** below and lets the agent drive: it calls
   precis tools to explore and, if warranted, to write.
4. On exit (agent stops, or turns/budget exhausted) writes one
   `dream_log` row (outcome, cost, turns, any `result_ref_id`s) and the
   full tool-call transcript into `dream_transcripts` (same `attempt_id`).

The agent's whole job is to leave the corpus a little better -- at
least one small change per run. It is not orchestrated through modes;
the behaviors
(consolidate / synthesize / TOC / inspire / acquire) emerge from which
tools it chooses.

### The prompt (deliberately minimal)

```text
You are dreaming over a personal knowledge base. Improve it a little.
Before you stop, leave at least ONE small change -- a note, a link, or a
conservative merge. Prefer small over sweeping.

You've been handed a FOCUS region (what's most due for a look) and a few
SPARKS (distinct, far-flung items), below -- no need to go find them.
Sit with the focus; glance at the sparks for an unexpected connection.

MAKE at least one change (small is good):
  - put(kind='memory', text=...)   a synthesis or inspiration note
  - link(...) / tag(...)           connect or label (e.g. DREAM:speculative)
  - supersede(merge_ids=[...])     merge near-dups (only compress, never invent)
  - acquire(identifier|title, ...) queue a missing paper to fetch

WANT MORE TO CHEW ON? (optional -- you already have enough to decide)
  - search(kind='*', like=<id>, angle=A, n=K)   K distinct items at
        cosine A from <id>:  A=1 same · A=0 unrelated · A=-1 opposite ·
        0<|A|<1 a random rotation that far out (nondeterministic)
  - search(kind='*', view='dreamable')          re-pull the focus region
  - search(kind='*', q=...)                     ordinary search
  - external: websearch / think / research      (only if it clearly helps)

When in doubt, make the smallest useful change -- then stop.

--- FOCUS REGION ---
{focus_region}

--- SPARKS ---
{sparks}
```

No strict output JSON, no per-turn contract: writes happen as the agent
calls the write verbs; the run ends when it stops or hits a limit.

### Turn & cost limits

Turns can be **large-ish** -- dreaming is a background job and we *want*
it to wander (read clusters, pull a ring, check an opposite, maybe one
external lookup). The real backstop is `--max-budget-usd` per run; turns
are a coarse safety net against pathological loops, not a tight leash.
Both default-off (whole feature gated by `PRECIS_DREAM_LLM`); knobs
`PRECIS_DREAM_MAX_TURNS`, `PRECIS_DREAM_MAX_USD`.

### Transcript capture

The full tool-call transcript (every search/get/put with args +
results) is written to a **`dream_transcripts` table** keyed by
`attempt_id` (1:1 with `dream_log`; see §Dream log). Kept in the DB
rather than a file: one transactional store, no orphaned files, no
extra path env var -- and at dreaming's low run volume the size is
negligible. The separate table keeps `dream_log` lean for analytics
scans; you `JOIN` only when you want the trace. This replaces the old
per-call `verdict` JSON: the transcript *is* the reasoning trace, and
`ref_events` still records the concrete writes.

### Write surface & safety

- **Additive verbs** (`put`, `link`, `tag`) are unrestricted -- a stray
  dream memory is inert clutter, trivially pruned by its `DREAM:` tag.
- **Consolidation is the one destructive behavior** and does *not* go
  through a raw `delete`. It routes through a dedicated **`supersede`**
  operation (delete + link-migration + `supersedes` edge + soft-delete,
  one tx -- see §Consolidate behavior) so it stays atomic and
  reversible. The agent cannot hard-delete anything.
- Dreams write with a `DREAM:` tag (and `DREAM:speculative` for
  inspirations) so every agent-authored row is identifiable and
  fenced from authoritative search by default.

## Dream navigation tools (MCP surface)

The cleverness lives here, in a handful of read tools. **No new verbs**
-- these are parameters on `search` (no new `cluster` kind),
both reachable over MCP and in-process.

### The `angle` spray on `search` (a diverse cone sample)

> **Implemented.** Pure anchor math in `precis/utils/angle.py`
> (`angle_anchors`, seedable RNG); the ANN-snap engine
> (`Store.angle_neighbours` / `_nearest_chunk`, card-inclusive) +
> seed resolution (`get_chunk_vector`, `seed_chunk_for_ref`) in
> `store/_blocks_ops.py`; dispatch interception
> (`PrecisRuntime._dispatch_angle`) routes `search` with `angle=`/`like=`
> away from the lexical+RRF path; the MCP `search` tool exposes
> `angle` / `n` / `like`. Tests: `test_angle.py`,
> `test_angle_search.py`, `test_angle_dispatch.py`.

`search` grows three optional params, on top of the existing cross-kind
fan-out (`kind='*'`):

- `angle` -- target cosine to the seed in `[-1, 1]`: `1` = same direction
  (the seed itself), `0` = orthogonal / unrelated, `-1` = opposite pole.
  Default `1` (today's nearest-first).
- `n` -- how many mutually-distinct items to return at that cosine.
- `like=<ref|chunk id>` -- seed by an existing item's stored vector
  instead of a `q=` string, so a dream pivots off a node.

The result is **not a cluster** -- it's `n` points at cosine `angle`
from the seed in *different directions*, each snapped to the nearest
real item. **One formula covers every angle:** for a unit seed `v` and
a random unit vector `u` drawn orthogonal to `v`,

```
w = angle*v + sqrt(1 - angle^2)*u      # cosine(w, v) == angle, exactly
```

Draw `n` random `u`s, build the `n` anchors `w_i`, ANN each, keep the
nearest real item, dedup. `angle=1` gives `w=v` (the plain
nearest-neighbour query); `angle=-1` gives `w=-v`. That is the whole
sampler -- a few lines of tensor ops plus the existing ANN call. High
dimensions make the random `u_i` near-orthogonal, so the items spread
on their own (no `diversify` flag).

> **Caveat (anisotropy).** Sentence-embedding spaces (BGE-M3 included)
> occupy a cone, not the full sphere -- vectors are mostly positively
> correlated, so a snapped item lands at the *realised* nearest cosine,
> not exactly `angle`. True cosine near -1 essentially never exists; the
> "opposite pole" is whatever is *least* similar (often cos ~ -0.1). An
> `angle` near 0.5 is well-populated; negative angles return the sparse
> contrarian fringe. Not a bug.

Angle presets (`view='unrelated'`, `view='opposite'`) are optional
sugar; raw floats remain available.

### `view='dreamable'`: the focus region (no `cluster` kind)

> **Implemented (no clustering dependency — sub-theming cut per scope
> decision 2026-06).** `Store.dreamable_region` picks the salience seed
> (`select_dream_seed`, `argmax(last_seen - last_dreamt)`) and returns
> its `n` nearest embedded chunks (card-inclusive, target kinds, live
> refs) — a single cosine ring, **not** an HDBSCAN/GMM carve-up. The
> runtime intercepts `search(view='dreamable')` in `_dispatch_dreamable`,
> stamps `last_dreamt` on every surfaced chunk (the rotation), and
> renders the region. Exposed on the MCP `search` tool via the new
> `view=` param. Salience is **not** bumped here (looking at a region
> counts as *dreaming* it). Tests: `test_dreamable.py`. The
> HDBSCAN/GMM retrieve-then-cluster path below remains deferred and is
> only needed if a single frontier must be split into labelled
> sub-themes in one call.

The focus region is just a `search` ranking mode (no new kind):

- `search(kind='*', view='dreamable')` -- return the region most
  **due** for a dream: pick the seed by `argmax(last_seen -
  last_dreamt)` over target kinds, ANN its neighbourhood, return that
  set (+ centroid for the worker's spray).
- Surfacing -- stamps `last_dreamt = now()` on the returned chunks
  (the rotation), so the region drops out and a different one tops next
  run.
- Drill in -- `search(like=<member id>, angle=1)` returns a member's
  neighbourhood; `get(id=...)` reads one item.
- Anything the run touches (focus, sparks, drilled items) is stamped
  `last_dreamt = now()` server-side at run end -- looking at it counts
  as dreaming it.

Clusters are computed by the **retrieve-then-cluster** machinery
(§Synthesize behavior): ANN-retrieve a salient frontier, cluster that
bounded subset. **Cluster ids are ephemeral by default** -- computed per
call, not persisted -- because the durable artifact is the dream + its
links, not a cluster table. (Open question: persist a refreshed
`clusters` table if stable ids across a run prove necessary for the
agent to reference a cluster it saw a few turns earlier; a cheap
alternative is to let the agent pin a cluster by its representative
member id via `like=`.)

### Where this sits in the seven-verb surface

No verbs added, **no new kind**. `search` gains `view='dreamable'` plus
`angle` / `n` / `like`; `supersede` is a guarded
operation on the `memory` handler (not a new top-level verb -- it is the
delete+migrate path of §Consolidate, exposed as a single tool so the
agent can't assemble it from raw `delete`). External providers are
already kinds. This keeps the surface migration intact.

## Consolidate behavior (memory-only)

### Schema migration `0007_dreaming.sql`

Additive only:

- Salience columns on `chunks` (above).
- Seed `relations`: `supersedes` (inverse `superseded-by`) and
  `superseded-by` (inverse `supersedes`); mirror in the `Relation`
  Literal + `_INVERSE_RELATIONS` in `store/types.py`.

`supersedes` is deliberately distinct from `retracts` (retraction =
"this was wrong"; supersession = "absorbed into a better phrasing").
The original is **soft-deleted, not hard-deleted** -- the `supersedes`
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
guarded operation** -- it never assembles a merge from raw `delete`:

```
supersede(merge_ids=[...],   # >= 2 live memory ids
          new_text="...",    # consolidated; <= inputs, no new claims
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
be live `memory` refs (no papers -- papers are never merged or deleted),
length ≥ 2, and the caller cannot target non-memory kinds. A bad call
fails with a typed error the agent can read and retry; it can never
corrupt or hard-delete.

**Conservative by construction (forget, don't invent).** A merge may
only **compress**: `new_text` must be no longer than the combined
survivors (the tool rejects a longer one) and may not introduce a claim
absent from them. The accepted failure is *losing a nuance*
(recoverable via soft-delete), never *manufacturing a false memory*.
When unsure, the agent drops detail rather than synthesising new
assertions.

**Audit.** Each `supersede` writes a `ref_events` row (source `'dream'`):
merged ids, link-migration counts, merged-into id -- plus the run's
`dream_log` row + transcript capture the surrounding reasoning.

### Soft-delete ↔ link semantics (checked against the code)

`supersede` soft-deletes the merged-away memories, so we verified what
that does to the graph (`store/_links_ops.py`, `store/_refs_ops.py`,
`handlers/_numeric_ref.py`). It is **safe and non-destructive**:

- **Non-cascading.** `soft_delete_ref` only sets `deleted_at=now()` on
  the ref; it never touches the `links` table. Link rows survive intact.
- **Links to a tombstone still resolve.** `links_for` joins
  `links → chunks` (for pos) but **never joins `refs`**, so it does not
  filter on the endpoint's `deleted_at` -- a link whose other end is
  soft-deleted is still returned.
- **Today: rendered with a marker.** The links view currently fetches
  endpoints via `fetch_refs_by_ids(include_deleted=True)` and
  `_format_link_line` appends `" (deleted)"` when `ref.deleted_at` is
  set -- so a link to a tombstone shows as `memory:<old> (deleted)`.
- **Fully reversible.** Because links are untouched, `deleted_at=NULL`
  on the old ref restores the original graph verbatim -- this is what
  makes "reversible at SQL" actually true.

**Decision (general, not dreaming-specific): hide links to deleted
endpoints by default, on both sides; reveal only on explicit opt-in.**
A reference to a deleted thing is clutter -- it shouldn't appear in the
normal links view in either direction (outbound *or* inbound). The
change is one predicate in the single read path, `Store.links_for`:

- Add `include_deleted_endpoints: bool = False`. LEFT JOIN `refs` on
  both endpoints and add
  `AND (include_deleted_endpoints OR (rs.deleted_at IS NULL AND rd.deleted_at IS NULL))`.
  Since the focal ref is alive, requiring both endpoints non-deleted
  reduces to "hide if the *other* end is deleted" -- covering out, in,
  and the inverse-relation rewrite uniformly.
- Internal callers that must see every edge (the future `migrate_links`)
  pass `include_deleted_endpoints=True`.
- User-facing reveal: an explicit flag on the links view (surface TBD --
  e.g. `view='links'` + `show_deleted=true`, or a `view='history'`).

Consequence for `supersede`: after `migrate_links` *hard-DELETEs* the
old ref's edges (re-pointed to the survivor), the only remaining pointer
to the dead ref is the `new → supersedes → old` provenance edge -- and
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
machinery below is what backs `search(view='dreamable')`; the agent
triggers synthesis simply by reading a salient cluster and, if it's
worth a summary, calling `put(kind='memory', ...)` + `link(...)`.

### Retrieve-then-cluster (scales past 500k, powers `view='dreamable'`)

Per `view='dreamable'` call:
1. **Seed selection.** The seed chunk is `argmax(last_seen - last_dreamt)`
   over target kinds (the date score, see Target selection) - no
   warm/idle split, no decayed `access_score`.
   - **Cold start:** with nothing accessed yet the date score ties at 0
     everywhere, so the earliest-`created_at` chunks seed first; the
     rotation spreads naturally as items get accessed.
2. **Frontier retrieval (HNSW, scales).** For each seed, ANN top-k →
   union into a working set of a few thousand chunks. Never loads the
   full matrix.
3. **Cluster the working set (cheap).** HDBSCAN/GMM (soft assignment)
   over the few-thousand-vector subset -- genuine structure, sub-second,
   memory-bounded regardless of corpus size. *(Adds `scikit-learn` /
   `umap` -- ADR-gated cross-package threshold, taken when clustering lands.)*
4. **Surprise is now a tool, not an env knob.** The old worker
   `PRECIS_DREAM_WILDCARDS` is gone -- in the agentic model the agent
   pulls a far-away or *opposite* member itself via the `angle` knob
   (`search(like=<centroid member>, angle=-1 | angle=0.5)`) when it
   wants grounded surprise (this is the Inspire behavior).
5. **Synthesis is the agent's write.** When a cluster is worth a
   summary, the agent writes a dream-origin memory with
   `put(kind='memory', ...)`, links it to its sources, and adds temporal
   keywords (ISO week/month) so dreams are time-addressable.

> **Relation decision (settled):** reuse the existing
> `derived-from` / `derived-into` pair for dream→source provenance. We
> do **not** add a `summarises` relation -- the distinction hasn't earned
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
memories, hundreds-thousands) → can cluster more globally; level 2+
→ tiny, fully global. Track height with `meta.dream_level`; children via `derived-from`.
This runs **automatically** in the normal rotation -- dream memories
are seeds like any other, no opt-in gate. Runaway isn't the worry:
dream memories are **low-value and droppable**, so stale,
never-revisited `DREAM:` memories can be pruned (aged out) without harm
and an unfruitful dream-on-dream simply ages out -- the bound is
acceptable loss, not a structural cap.

**Prune pass (the actual bound).** A periodic sweep soft-deletes
`DREAM:`-tagged memories whose `last_seen` never advanced past
`created_at` after a grace window and that nothing links to as a
promoted source. Reversible like any soft-delete (`deleted_at=NULL`),
audited in `ref_events` -- this is what keeps automatic recursion from
accumulating.

### Dreams in search (fenced, never boosted)

> **Implemented (the `DREAM:speculative` fence).** All three block-search
> paths (`search_blocks_lexical` / `_semantic` / `_fused` in
> `store/_blocks_ops.py`) exclude refs tagged `DREAM:speculative` by
> default via a parameterless `NOT EXISTS` clause
> (`speculative_fence` in `store/_tag_filter.py` — param-free so it
> survives the fused CTE's double-splice of the shared WHERE). The
> fence lifts when the caller forces `include_speculative=True` or
> lists `DREAM:speculative` in `tags=` (listing the control tag *is*
> the opt-in). Consolidated dream memories carry no speculative tag,
> so they stay visible and unboosted. No-op for kinds that never carry
> the tag. Tests: `test_speculative_fence.py`.

Dream-origin memories are ordinary `memory` rows in fused search -- no
special ranking bump. They are `DREAM:`-tagged so they can be fenced
from authoritative results, and always carry `derived-from` links so a
reader can drill to primary sources (no dead ends; survives cold
start). Re-dreaming needs no scheduler: the `last_seen - last_dreamt`
rotation re-surfaces a churned region on its own.

## Navigate / TOC behavior (a shape of synthesize)

Not a separate behavior so much as a *shape* of the synthesis write:
instead of a prose summary, the agent emits a **navigable outline**
memory over a region -- reusing precis's existing TOC vocabulary
(`segment_toc` worker, `ref_segments`, `views=("toc", …)` on handlers),
but at cross-ref / region scope instead of per-ref.

- **Output:** a memory whose body is an outline; each entry =
  `<subcluster gloss>` + `derived-from` links to representative source
  chunks/refs ("expound with context" = gloss + drill-down, not a wall
  of prose).
- **Guards:** cap entry count; only expound subclusters above a
  size/salience floor so a TOC doesn't balloon.
- **Shares everything else** with synthesis: `view='dreamable'` and the
  recursion (`meta.dream_level`). The agent just chooses an
  outline-shaped `put` when a region is better navigated than
  summarized.

## Inspire behavior (speculative, fenced)

The creative-recombination behavior -- and the reason the `angle` knob
exists. The agent:

- Picks the **focus region** (`search(view='dreamable')` =
  "what we're worried about now").
- Pulls **remote/opposite stimuli** with the knob itself:
  `search(like=<cluster member>, angle=0.5)` for moderately-distant
  ideas or `angle=-1` for the opposite pole -- cross-kind
  (`kind='*'`) so any kind can fertilize.
- Judges, in its own head, whether there's a *realistic* way to apply a
  stimulus to the issue. Bias: say no *per stimulus*. Most reads → nothing, and that's fine; the run still leaves its one small
  change elsewhere (synthesis, link, or merge) rather than forcing a
  weak inspiration.
- **On a real hit:** `put(kind='memory', ...)` a low-confidence note
  tagged `kind:inspiration` + `DREAM:speculative`, then `link` it to
  *both* parents (issue cluster + stimulus) so the provenance -- "this
  idea came from applying X to Y" -- is traceable.

**Fencing (decided):** inspirations are NOT boosted in default search
and do not pollute authoritative results. They surface on explicit ask
or a dedicated view, and the `DREAM:speculative` tag makes pruning
trivial. Later consolidation or a human can promote the good
ones.

**Autonomy:** autonomous-write is acceptable *because* it's fenced and
non-destructive -- a bad inspiration is inert clutter, never a
corruption. Cost is the main risk: tightest per-call budget, lowest
frequency, default-off.

## Acquire behavior (surface & fetch missing papers)

A dream is well-placed to notice the corpus is *missing* a paper it
keeps bumping into. The decisive fact: **the fetch pipeline already
exists end-to-end** (see §grounding) -- so the agent only mints a stub,
and everything downstream is automatic.

### The `acquire` tool (guarded, like `supersede`)

> **Implemented (handler method; MCP/agent wiring deferred to #8, like
> `supersede`).** `PaperHandler.acquire(identifier=, title=, reason=,
> context_ref_id=)` in `handlers/paper.py`: parses `doi:`/`arxiv:`/`s2:`
> (or a bare DOI / arXiv id), best-effort S2 enrichment via the
> patch-out-able `_lookup_acquire_metadata` (never raises — the stub
> mints offline too), then idempotently upserts a stub through the new
> `Store.upsert_stub_paper` (identifier-collapse; mirrors the chase
> stub path), tags a *fresh* stub `DREAM:acquire` with
> `meta.set_by='dream'`, and links it from `context_ref_id`. Already-held
> papers short-circuit to a no-op and are never re-tagged. The
> `DREAM:acquire` closed value + `DREAM` on the `paper` axis are
> registered in `store/types.py`. Tests: `test_acquire.py`.

```
acquire(identifier=<doi|arxiv|s2> | title=...,   # what to get
        reason="...",                            # why it's worth holding
        context_ref_id=<ref>)                    # where it came up
```

It does the minimum, then gets out of the way:

1. Resolves metadata via S2 (`get_paper_by_id` / `lookup_s2`) -- enough
   to mint a meaningful stub (title, year, ids).
2. Upserts a **stub paper ref** *idempotently* (identifier-collapse via
   `ref_identifiers`: a hit on an already-held or already-wanted paper
   short-circuits to a no-op), with `meta.set_by='dream'` and a
   `DREAM:acquire` tag -- reusing the exact path `chase` already uses
   (`_resolve_or_create_stub`).
3. Links the stub to `context_ref_id` (provenance) and to the dream.
4. Returns immediately. **It never ingests inline** -- no Marker, no
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
  `precis stubs` renders -- *the required-papers list*. New wants append
  in stub-creation order; a human (or the `doilist` operator flow)
  drains it, and if an OA copy later appears `fetch_oa` grabs it
  automatically.

### Gating & safety

Behind its own `PRECIS_DREAM_ACQUIRE` gate. Minting a stub is additive
and reversible (soft-delete), and because heavy ingest runs in the
normal worker rotation, a runaway dream can at worst enqueue paper
stubs -- it can never blow the run budget on downloads or ingest. The
`acquire` tool, like `supersede`, is the *only* way the agent touches
the paper-acquisition path; it cannot drive `fetch_oa` or `precis add`
directly.

## External search (dreams reaching outward)

Dreaming need not be closed-corpus. A dream can pull in *new* external
knowledge -- to ground a speculative inspiration (has someone already
done this? what's the prior art?) or to fetch a reference a cluster
keeps citing but we don't hold.

**No special plumbing -- they're already MCP kinds.** Semantic Scholar
and Perplexity are existing precis kinds (`websearch` / `think` /
`research`, plus the S2 ingest path), each cache-backed: a call parses,
embeds, and persists a ref + `cache_state` row, so results are
**immediately searchable and linkable** and re-asking is free. In the
agentic model the dream just *calls them like any other tool* --
`get(kind='websearch', q=...)` -- and the caching, dedup, and cost
tracking come for free from the handler. (This is why agentic-over-MCP
doesn't sacrifice cache discipline: the cache lives in the handler,
hit via `runtime.dispatch` regardless of caller.)

**Cost is a hint, bounded by the run.** The prompt nudges toward the
cheap tiers -- free Semantic Scholar and ~$0.001 `websearch` first,
`think` when it helps, deep `research` only when clearly worth it -- but
there's no per-search ceiling; the only hard cap is the run's overall
`--max-budget-usd`. Gated behind `PRECIS_DREAM_SEARCH` (separate from
the `PRECIS_DREAM_LLM` master gate) so external reach can be disabled
without disabling dreaming. Warrants its own ADR.

## Dream log (telemetry & feedback loop)

One row per **agentic run** (not per micro-decision -- the agent's
internal exploration lives in the transcript). It records what a run
did and what it cost; the full trace lives in a sibling
`dream_transcripts` row. Together they're the substrate for "optimize
the dreaming".

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
  summary          JSONB                 -- agent's closing note + counts
);

-- 1:1 sibling, kept separate so dream_log stays lean for analytics scans
CREATE TABLE dream_transcripts (
  attempt_id       BIGINT PRIMARY KEY REFERENCES dream_log(attempt_id),
  transcript       JSONB NOT NULL        -- full tool-call trace (args + results)
);
```

- **Keep everything forever.** No pruning, no retention window. In the
  eval phase a `noop` outcome should be rare (a run cut off by
  turns/budget before committing its one change); it is still logged --
  the looked-but-didn't-write trace is a tuning signal. Nothing here
  surfaces in normal search; it's analysis-only.
- **The transcript is the rich record.** Fine-grained provenance --
  *which* remote/opposite stimulus led to an inspiration -- comes from
  (a) the `derived-from` links on the created inspiration (both parents
  linked) and (b) the saved transcript, not from a structured
  `stimulus_dist` array. This is the cost of going agentic: telemetry
  moves from typed columns to the trace.
- **Analytic dimensions** still queryable cheaply: fruitfulness
  (`wrote / total`), behavior mix (`behaviors`), cost/turns per run.
  Deeper "what fertilizes what" analysis parses transcripts offline.
- **Volume is fine:** dreaming is gated, default-off, low-frequency, so
  one `dream_log` row + one `dream_transcripts` row per run is negligible.
- Indexes: `(outcome, created_at)`; optional GIN on `behaviors`.

## CLI wiring

A single `--only dream` pass in `cli/worker.py`, default-off in the
normal rotation (like `fetch_oa`). There is **no `--dream-mode` switch**
any more -- one agent chooses its own behavior. The pass launches the
`claude` binary with the precis MCP config, `--max-turns`, and
`--max-budget-usd`, then records the run. Env knobs:
`PRECIS_DREAM_LLM` (master gate), `PRECIS_DREAM_MODEL` (opus-class),
`PRECIS_DREAM_MAX_TURNS`, `PRECIS_DREAM_MAX_USD`,
`PRECIS_DREAM_SEARCH` (external-reach gate),
`PRECIS_DREAM_ACQUIRE` (paper-stub-minting gate),
plus the clustering-frontier size. (Transcripts go to the
`dream_transcripts` table, not a file -- no path env var. Target
selection itself is **knob-free** -- `argmax(last_seen - last_dreamt)`,
see §Target selection.) (`PRECIS_DREAM_WILDCARDS` /
`STIMULUS_MODE` / `MAX_DIST` are retired -- surprise is now an `angle` spray, neighbour distance the `angle` arg.)

## Test plan

Most behavior is now the agent's, so tests target the **tools** (which
are deterministic and in-process testable) and the **run harness**, not
LLM judgment.

- **Target selection:** `score = last_seen - last_dreamt`; assert
  `argmax` picks the most-due chunk, surfacing a region sets its chunks'
  `last_dreamt=now` so it drops out and a *different* region tops next
  run (rotation, never an empty set), a freshly-accessed chunk jumps the
  queue, and **dream-actor reads do not advance `last_seen`** (feedback
  guard). Migration backfills both timestamps to `created_at` (both
  `NOT NULL`, no NULL branch); `accesses` increments for heatmaps only
  and never enters the ranking. Scope: targets are `paper`+`memory`
  only; `oracle` appears as a spark, never a target; `skill` excluded.
- **Angle spray:** `angle=1` is today's nearest search (byte-identical);
  `angle=-1` returns the nearest neighbour of `-v`; for `0<|angle|<1`
  each anchor `w = angle*v + sqrt(1-angle^2)*u` has cosine `angle` to `v`
  by construction, and the `n` snapped items come back in distinct
  directions (dedup) at their realised nearest cosine; `like=<id>` seeds
  from the stored vector.
- **`view='dreamable'`:** ranks by `argmax(last_seen - last_dreamt)` and
  returns the seed's region; surfacing stamps `last_dreamt`; there is no
  `cluster` kind; cross-kind fan-out (`kind='*'`) merges via RRF
  (existing behavior, regression-guarded).
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
  exclusion -- catches a missing `deleted_at IS NULL` predicate);
  `deleted_at=NULL` restores the original (visible) link graph verbatim.
- **Agent harness:** mocked `claude` (`PRECIS_CLAUDE_BIN` stub) that
  emits a scripted tool-call sequence → assert writes happened; a
  **budget-exhausted run** (agent reads but is cut off before writing)
  writes a `dream_log` row with `outcome='noop'`, empty
  `result_ref_ids`, and a `dream_transcripts` row; `--max-turns` /
  `--max-budget-usd` truncation recorded.
- **Fencing:** dream-origin memories are plain `memory` rows in fused
  search (no special boost); `DREAM:speculative` inspirations are
  excluded from default results; `derived-from` drill-down works.
- **`acquire` tool:** mints a `pdf_sha256 IS NULL` stub tagged
  `DREAM:acquire` with `meta.set_by='dream'`; idempotent on a known
  identifier (re-acquire = no-op); links to context; the stub is
  claimable by `claim_stubs_to_fetch` (regression: a dream stub and a
  chase stub are indistinguishable to `fetch_oa`); never ingests inline.

## Definition of done (per AGENTS.md)

- Plan reviewed (this doc).
- ADRs in `docs/decisions/`: (a) `supersedes` relation + the guarded
  `supersede` tool (additive-only agent writes, no raw delete);
  (b) salience model + columns on `chunks` + the `dream_log` table;
  (c) the `angle`/`n`/`like` knob on `search` (cone-sampling) and
  `view='dreamable'`, incl. the clustering dependency
  (`scikit-learn`/`umap`) -- taken when clustering lands;
  (d) the **agentic dream loop** (claude-over-MCP, turn/cost-bounded,
  transcript capture, default-off) -- the headline capability ADR;
  (e) external-reach gate (`PRECIS_DREAM_SEARCH`); (f) acquire behavior
  -- the guarded `acquire` stub-minting tool + `PRECIS_DREAM_ACQUIRE`,
  reusing the chase-stub / `fetch_oa` loop.
- `0007_dreaming.sql` applies cleanly to a fresh DB (salience columns,
  `bump_salience()`, `supersedes` relation, `dream_log` +
  `dream_transcripts` tables); only the new file pending under
  `precis migrate --dry-run`.
- Navigation tools shipped + tested in-process (`angle`/`n` spray,
  `view='dreamable'`, `supersede`); `--only dream` has `--help`, an
  integration test (mocked `claude`), and a README line.
- The agent loop runs end-to-end against the MCP server with a real
  (gated) model at least once; a budget-exhausted run and a writing run
  both produce correct `dream_log` rows + transcripts.
- Full check green (`ruff check`, `ruff format --check`, `mypy`,
  `pytest`); version bump + `CHANGELOG` entry.

## Suggested sequencing

Tools first (deterministic, independently useful), then the agent on
top.

1. Salience columns + the `bump_salience()` function (useful on its own
   for search ranking).
2. Memory `card_combined` chunks + backfill + `supersedes` migration +
   `dream_log` table.
3. The `angle`/`n`/`like` knob on `search` (one rotation formula:
   `w = angle*v + sqrt(1-angle^2)*u`; `-v` for the opposite pole).
   Useful to any caller, not just dreams.
4. `view='dreamable'` (retrieve-then-cluster behind `search`) + the
   `supersede` tool. Behind the clustering-deps ADR.
5. Fencing (`DREAM:` tag) for the dream/memory layer.
6. **The agent loop** (claude-over-MCP, `--max-turns`/budget, transcript
   capture, `dream_log`) behind `PRECIS_DREAM_LLM`. This is where the
   behaviors come alive.
7. External reach: flip `PRECIS_DREAM_SEARCH` (providers are already
   kinds) + its ADR.
8. Acquire: the `acquire` stub-minting tool + `PRECIS_DREAM_ACQUIRE`
   (the `fetch_oa` / `precis stubs` machinery already exists, so this is
   just the guarded tool + a gate).

## Open questions for the reviewer

1. Access-event weights (cite / read / search-impression) -- starting
   values?
2. `cluster` ids: ephemeral (computed per call) vs a persisted,
   refreshed `clusters` table -- needed only if the agent must reference
   a cluster it saw several turns earlier (the `like=<member>` pin is a
   cheaper alternative). Start ephemeral?
3. `--max-turns` default (large-ish) and per-run `--max-budget-usd`
   default -- pick starting values; budget is the real backstop.
4. Transcript format + retention: full session JSON vs a trimmed
   tool-call log; keep forever or age out old transcripts?
5. Cluster-frontier size and `angle`/`n`/cone defaults -- pick
   conservative values and tune from `dream_log` telemetry.

_Resolved:_
- **Full-agentic** loop (claude-over-MCP, turn/cost-bounded) is the
  design; there is no separate per-mode worker path.
- **No action is a first-class outcome**; memories are low-value and
  reversible, so trust the model and bias toward doing nothing.
- **Writes are additive** (`put`/`link`/`tag`); destructive merges go
  only through the guarded `supersede` tool (no raw delete).
- **Angle knob** (`angle`/`n`) is feasible: `-v` for the pole,
  cone-sampling for the mid-cone; anisotropy means true `-1` is rare.
- **Search spans all kinds** via the existing `kind='*'` fan-out.
- **Provenance relation** = reuse `derived-from` (no new `summarises`).
- **Transcript to a DB table** (`dream_transcripts`); `dream_log` is
  one row per run + a pointer.
- External reach: providers are already kinds; cost is a prompt hint
  bounded by the run budget; gated by `PRECIS_DREAM_SEARCH`. (Patents
  deferred -- kind exists but currently unavailable.)
- **Acquire = auto-fetch, gated.** The dream mints a paper stub; if an
  OA copy exists the existing `fetch_oa` worker auto-ingests it, else it
  appends to the `precis stubs` required-papers backlog. No inline
  ingest; gated by `PRECIS_DREAM_ACQUIRE`.
- `dream_log` retention -- keep everything forever (incl. no-ops),
  analysis-only.
