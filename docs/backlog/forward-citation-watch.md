---
status: draft
title: Daily OpenAlex watch for new papers citing papers we hold and use; queue them for fetch
pillar: memory-graph
prio: normal
---

# Forward-citation watch (gr513769)

## Motivation / why

Reto, 2026-10-24: discover when papers that just came out cite papers we
already hold, "and ideally are repeatedly using", and queue them. Today the
cited-by side is S2-only and either manual (`scripts/find-citing-papers`,
`scripts/_find_citing_papers.py`, a report + JSONL feed) or one-shot per
first read (`src/precis/workers/inbound_chase.py`, dark). Neither runs daily
or weights by use. OpenAlex answers "who cites these N works" in one batched
filter call and the project already holds an OpenAlex client and a shared
rate lane.

Constraint: the stub queue is ~8k and minting outpaces fetch ~7:1 (gr513769
body). This item must not add to that imbalance, and shares the budget with
the gr513755 stub trickle.

## In scope

1. **Seed set — held papers weighted by use.** Defining "use" (checked
   against the tree: there is **no access counter** on refs):
   - `refs.last_recalled_at` — any agent `get` of the ref
     (`store/_refs_ops.py::touch_recalled`, throttled to once an hour, so it is
     recency, not count).
   - `refs.last_viewed_at` — human web reader page-load
     (`touch_viewed`, `precis_web/routes/papers.py`), recency only.
   - Inbound links from live `finding`, `draft`, `quest` refs (a finding cites
     the paper, a quest serves it) — durable, countable, survives the
     recency decay.
   - `INBOUND:swept|pending` tag — "ever read"; too coarse to weight.
   **Chosen signal:** `w = 3·(distinct findings/drafts linking the paper) +
   2·(1 if a live quest links it) + recency`, `recency = 1` when
   `GREATEST(last_viewed_at, last_recalled_at)` is within 90 days, 0.5 within
   a year, else 0. Reason: links are the only count we have and they mean
   "relied on", not "opened once"; recency is the only usage signal that
   exists for reads and is cheap (one column). Adding a real access counter
   is rejected here (new write on every read, schema change) — open question 1.
   Top N seeds (500, accepted by Reto 2026-10-24; a setting), each with an OpenAlex id from
   `ref_identifiers` (`id_kind='openalex'`, filled by `enrich-openalex`); a
   seed lacking one is skipped and counted, not resolved inline.
2. **Daily poll.** One pass per day (cadence throttle via
   `src/precis/workers/_throttle.py::due` and an `app_state` marker, advisory
   single-runner lock — the `openalex_enrich.py` pattern). Batches of seed
   ids per request: OpenAlex `works?filter=cites:W1|W2|…` (OR list; batch ≤ 50,
   limit to confirm against current OpenAlex docs), `sort=publication_date:desc`,
   cursor-paged, window `from_publication_date=<last successful poll −
   3 days>` (3-day overlap accepted 2026-10-24; absorbs OpenAlex's indexing lag; dedup makes re-seeing
   harmless). 500 seeds ≈ 10 requests plus pages, negligible against the
   `openalex` lane's 100k/day quota.
3. **Client + policy reuse.** New `fetch_openalex_citers(ids, since, …)` beside
   `fetch_openalex_work` in `src/precis/ingest/openalex_meta.py`: same
   `call_with_backoff(service="openalex")`, same shared `openalex` limiter row
   (`rate_limit.require` → `RateLimited`), same UA/mailto. The outbound class is
   `provider-api` (`src/precis/utils/outbound_policy.py`, the `openalex`
   row); no new class, no new row. A `RateLimited` refusal ends the pass
   unstamped (next day retries), as `openalex_enrich` does.
4. **Score and queue.** For each new citing work: `score = Σ w(seed) over
   the seeds it cites` (the response carries `referenced_works`; intersect
   with the batch's seed ids). Skip works with no DOI and no
   arXiv/openalex id. No quest-relevance gate yet (on hold, question 2):
   queue on citation score alone. Take the top `K` per day by score
   (`K` = the day's cap, below); mint through `Store.upsert_stub_paper`
   (identifier-collapse find-or-mint) so `fetch_oa` auto-claims it. Set the
   stub's `prio` and `meta.prio_by='forward_watch'`: `stub_rank` leaves any
   non-null `prio` with a different `prio_by` untouched
   (`src/precis/workers/stub_rank.py` rank guard), so the watch's ordering is
   not overwritten. Quest relevance is **not** recomputed here; `stub_rank`'s
   anchor similarity still scores the stub's embedding for display.
5. **Edge as audit trail.** Write paper→paper `cites` (new stub → each seed it
   cites) via `add_link`, `meta={'source': 'forward_watch', 'date': …}`.
   Cited-by then reads for free on the seed (`links_for` direction `in`) and
   the digest queries it.
6. **Dedup.**
   - Already held or already a stub: `upsert_stub_paper` returns the existing
     ref (`created=False`) by DOI / arXiv / openalex identifier; the watch
     adds only the `cites` edge. So a held citer gains an edge at zero cost.
   - Window overlap and reruns: idempotent through the same find-or-mint and
     `add_link`'s unique tuple.
   - Retired/merged refs: identifier collapse follows the survivor (as
     `resolve_citation` does for `supersedes`).
   - Works with no usable identifier are skipped and counted, not retried
     endlessly (a `meta` stamp on the seed or the same ledger store, slice 3).
7. **Cap and the gr513755 trickle (shared budget).** One daily stub-mint
   budget `B` covers both producers, held in one `app_state` counter
   (`stub_mint_budget:<UTC date>`), both consumers `reserve` from it:
   - `B = min(B_max, floor(trailing-7-day fetch landings / 7))`: minting never
     exceeds what `fetch_oa` actually lands per day, so the queue cannot grow
     faster than it drains. `B_max` = 100/day (accepted 2026-10-24), a
     setting.
   - Watch takes first claim, at most `B/2`; the trickle takes the rest and
     whatever the watch left unused.
   - Both also self-pause when open stub count (the `precis-stubs-help`
     backlog) exceeds a high-water mark (default 10,000), and resume below a
     low-water mark — minting stops entirely until the 7:1 backlog drains.
   - Over-cap candidates go on a **ledger** ("put them on the list anyway",
     Reto 2026-10-24): one row per citing work (openalex id, DOI/arXiv id if
     any, score, seed ids, first-seen date), no stub minted. Later days draw
     from the ledger first, highest score first, before new candidates, within
     `B`. Entries drop when the work becomes held/stubbed by any path, or
     after 180 days unqueued. Storage (an `app_state` blob vs a small table,
     forward-only migration) is chosen in slice 3. The digest reports "N more
     on the list".
8. **Digest line.** The daily composed digest is the morning reading brief:
   `src/precis/reading/briefing_cast.py::_lane_system_activity` (the
   "SYSTEM ACTIVITY OVERNIGHT" lane). Add one line, e.g. "3 new papers cite
   your MCB sources (2 queued, 1 over cap)", with the seeds collected via
   `_collect(sources, …, "cites")` so the draft links back to them. Data
   source: stubs / edges stamped `forward_watch` with `created_at` > the lane
   cutoff. `health_digest` (SQL ops alarms) is not the home: this is content,
   not a health signal; a "watch did not run / limiter refused for N days"
   alarm does belong there, via the existing staleness conditions.

## Explicitly NOT in scope

- Historical cited-by backfill (the OpenAlex/S2 snapshot decision in
  gr513755).
- The reference-list side (`referenced_works` sourcing, `held_ref_id` link
  writer, trickle) — gr513755.
- Chunk-level resolution of the new citers (`citation-resolution-skill.md`).
- Fetching the PDFs: stubs are queued; `fetch_oa` owns acquisition.
- Retiring `scripts/find-citing-papers`: it stays as the S2 cross-check
  (Reto 2026-10-24).
- A quest-relevance gate (on hold).
- A new access counter on refs.

## OpenAlex paid key — dependency

The enrich path does not send a key: `fetch_openalex_work` passes `mailto`
only (polite pool). The only key in the tree is `PRECIS_OPENALEX_CONTENT_KEY`
(`src/precis/workers/fetch_oa.py::_openalex_content_key`), used for paid
content downloads and the credit-balance check. Whether it is configured in
prod is not visible from the repo (secret); the coordinator is checking.

- Slices 1-4 **do not need the paid key**: ~10-20 keyless requests a day sit
  far inside the polite pool and the shared 100k/day lane.
- It **would** matter if (a) the polite pool is saturated by the other
  OpenAlex passes (gr513755 Reto note: the free daily limit may be what stops
  the enrich), or (b) OpenAlex restricts a filter this watch wants
  (`from_created_date` / `from_updated_date`, which catches late-indexed
  works better than `from_publication_date`, are documented as key-gated;
  unverified — check before relying on them). In either case the client gains
  an `api_key` param read via the same secret path, and the key must stay out
  of logs (`utils/http.py` already redacts `api_key` query params).

## Acceptance criteria

- Dry run (`--dry-run`) prints the seed list with weights, the batch
  requests, and the would-be stubs, and writes nothing.
- A live pass on the dev DB with a fixture seed set mints at most `B` stubs,
  adds `cites` edges for held citers without minting, and a second run the
  same day mints nothing.
- Killing the limiter row (daily_cap spent) ends the pass unstamped and logs
  once; next run resumes.
- Total of watch + trickle mints per UTC day ≤ `B`; with open stubs above the
  high-water mark both mint 0.
- Digest line appears in the next morning brief only when the watch queued or
  over-capped ≥ 1 paper; absent otherwise.
- Pass is default off (ship dark), like `inbound_chase`.

## Slices (smallest first)

| # | Slice | Acceptance |
|---|---|---|
| 1 | Seed scoring query + `--dry-run` CLI listing seeds, weights, skipped-for-no-openalex-id count. | Matches a hand-computed fixture; no network; read-only. |
| 2 | `fetch_openalex_citers` in `ingest/openalex_meta.py` (batched `cites:` filter, cursor paging, shared limiter) + dry-run of citers per seed batch. | Mocked-HTTP tests: batching, paging, 404/429/5xx via `call_with_backoff`, `RateLimited` propagates; dry-run lists citers with scores. |
| 3 | Mint + edge + dedup: `upsert_stub_paper`, `prio_by='forward_watch'`, `cites` edge, rerun idempotence, over-cap ledger (storage choice, draw order, expiry). | Second run same window mints 0; held citer gains edge only; `stub_rank` leaves prio untouched; over-cap citers land on the ledger and are drawn highest-score-first on later days before new candidates; a ledger entry that became held/stubbed elsewhere is dropped. |
| 4 | Shared daily budget `B` with the trickle: counter, trailing-fetch formula, high/low-water pause, watch-first split. | Property test: any interleaving of the two producers stays ≤ `B`; pause/resume at the marks; budget refused ⇒ both defer, neither errors. |
| 5 | Worker pass: cadence throttle, advisory lock, `app_state` last-run marker, registry entry default off, staleness condition for `health_digest`. | Single runner across hosts; skipped when throttled; alarm after N missed days. |
| 6 | Digest line in `_lane_system_activity` with source links. | Lane test: line present with counts and links when a pass queued papers; absent otherwise; lane still degrades to empty on error. |
| 7 | Optional: key-gated filters (`from_created_date`) if the key is configured and the 3-day overlap proves leaky. | Only after a week of slice 5 data shows missed citers. |

## Target + blast radius

New `src/precis/workers/forward_watch.py`; additions to
`ingest/openalex_meta.py`, `workers/registry.py`, `reading/briefing_cast.py`,
a `cli` dry-run entry. Coupling with gr513755: the budget counter is shared
code that both producers import — land slice 4 before either produces at
volume. Load: ~10-20 OpenAlex requests/day, ≤ `B` stub rows/day, a few
hundred `links` rows/day at most. Blast radius if wrong: stub-queue growth
(bounded by `B` and the water marks) and one wrong digest line.

## Open questions / decisions log

Decided (Reto 2026-10-24, relayed by COORDINATOR):

- Defaults accepted: N=500 seeds, 100 stubs/day ceiling, 3-day window
  overlap.
- Over-cap citers are kept on a ledger and drawn on later days.
- Relevance gate on hold: queue on citation score alone.
- `scripts/find-citing-papers` stays as the S2 cross-check.

Open:

1. **Use signal.** Is the link-count + recency weighting right, or should
   reads count? A true access counter needs a schema change and a write per
   read; the 1-hour throttle on `last_recalled_at` already makes it recency
   only. Default here: no counter.
2. **Relevance gate — ON HOLD.** Queue on citation score alone for now;
   whether to also require similarity to an active quest before spending a
   fetch is unruled (`stub_rank` already ranks the queue by quest anchors).
3. **Paid key.** Pending the coordinator's check; see the dependency
   section. If unconfigured and the polite pool is already exhausted, slice 2
   cannot ship without it.
