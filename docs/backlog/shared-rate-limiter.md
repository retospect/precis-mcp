---
status: in-progress
title: Shared cross-host rate limiter for outbound external APIs (keyed, two-lane)
pillar: platform
---

# Shared rate limiter — coordinate outbound API access cluster-wide

## Problem

No shared/DB-backed rate limiter exists for external HTTP. `resource_slots`
(`llm:*`) coordinates only LLM backends; `utils/safe_fetch.py` is an SSRF
guard, not a limiter. The concurrency axis is `default_profiles` in
`workers/registry.py`: a `_SYS` pass runs on **every** worker host (~5) at
once, each draining rows `FOR UPDATE … SKIP LOCKED` and issuing one external
call per item → **uncoordinated concurrent cluster access**. Per-thread
tenacity backoff does not coordinate across hosts, so the fleet collectively
breaches a provider's rate even while each thread politely backs off. S2
(~1 rps ceiling, aggressive 429s) is the acute case.

## Scope (confirmed by outbound-API survey, 2026-08-11)

The concurrent-cluster problem is **confined to the scholarly/OA passes**.
Everything non-scholarly is either on-demand MCP (ORCID, Wikipedia, YouTube,
Wolfram, Perplexity, web — one process, agent-paced, self-limiting) or already
coordinated (EDGAR ships its own in-process `TokenBucket` at SEC's 10 rps;
Espacenet's `epo_ops` SDK throttles). Those are **excluded** — bucketing them
only adds latency.

Genuine seed set (passes that run `_SYS` against a shared endpoint):

| Provider | Lane | Rate / cap | Notes |
|---|---|---|---|
| **s2** | rate | ~1 rps | top priority; `chase`+`stub_rank` every-node |
| **openalex** | rate + **daily** | ~8 rps; **100k/day** | hard daily cap |
| **unpaywall** | **daily** | 100k/day | no real rps limit |
| **arxiv** | rate | ~1 req/3s per-IP | concurrent PDF pulls |
| **crossref** | rate (low pri) | polite pool | 5 scattered callsites, high wiring cost |

**Key insight — the generalization is TWO LANES, not more providers.** A token
bucket smooths *rate* but cannot stop *daily-quota* exhaustion. The limiter
carries a rate lane (token bucket) **and** a daily-quota lane (counter) per
provider key.

## Design

- **Table** `external_rate_limits` (migration 0121): `provider` PK, `capacity`,
  `refill_per_sec`, `tokens`, `last_refill`, `daily_cap` (NULL = no quota lane),
  `day_used`, `day_start`. One row per provider; the **single-row atomic
  `UPDATE … FOR UPDATE` is the cross-host coordination point** (analogous to
  `resource_slots`, but rate/quota-based).
- **Module** `precis/utils/rate_limit.py`: `acquire(provider, *, n=1,
  max_wait_s=30.0) -> bool`. Own lazy DB pool from `load_config().database_url`
  (store-free, so S2 fetch fns that lack a `store` can call it). **Fail-OPEN**
  on every error path (flag off / no DSN / no row / DB down) — degrades to
  today's uncoordinated tenacity behavior, never wedges a worker. Bounded,
  jittered wait when rate-starved; immediate False when a daily cap is
  exhausted. Flag `PRECIS_RATE_LIMIT` (default on).
- **Wiring (v1 = S2 only, rate lane).** `acquire("s2")` at each S2 HTTP call
  site inside its tenacity-retried fn (retries re-acquire): `ingest/citations.py`
  (`_get_references`/`_get_citations` + batch/pagination fns),
  `ingest/semantic_scholar.py` (`lookup_s2`/`get_papers_batch`/`get_paper_by_id`/
  `search_s2_papers`), and the straggler `ingest/fetch_oa.py:~2683` (builds its
  own S2 client). `handlers/semanticscholar.py` (on-demand MCP) excluded.

## Chokepoint / wiring cost (for future providers)

- **Unpaywall, arXiv** — single chokepoint each in `fetch_oa.py`
  (`_query_unpaywall` / `_try_arxiv`). Cheap.
- **OpenAlex** — `ingest/openalex_meta.py` (enrich) + three `fetch_oa.py`
  helpers. Needs BOTH lanes wired (rate + the 100k/day counter).
- **Crossref** — scattered across 5 callsites / 3 clients (habanero, safe_get,
  raw httpx) → highest wiring cost; low priority, hence rate-only + last.

## Status

- **Shipped:** table + module (both lanes) + S2 rate wiring; OpenAlex wired
  on both lanes through `rate_limit.require()` (raises `RateLimited` on a
  refusal, so a spent daily quota skips the request instead of firing it):
  `openalex_meta.fetch_openalex_work` plus the three `fetch_oa.py` helpers.
  `openalex_enrich` stops its batch on the first refusal; `fetch_oa` records
  `rate_limited` with `limiter: shared`; `paper_meta_enrich` re-raises it
  and defers the batch unstamped. Deliberately ungated: the `/rate-limit`
  balance check and the web key probe (low volume, outside the works quota).
  Unpaywall wired on its daily lane: `fetch_oa._query_unpaywall` calls
  `require("unpaywall")`; a refusal records `rate_limited` with
  `provider: unpaywall, limiter: shared` and sends no request. arXiv wired
  on its rate lane: `_try_arxiv` and the arXiv HTML/source markup legs
  (`_MarkupLeg.limiter`) call `require("arxiv")`; a refusal records
  `rate_limited` and skips the download. Crossref wired on its rate lane at
  the three `_SYS` pass chokepoints: `paper_meta_enrich.enrich_paper`
  (batch deferred unstamped), `bib_parse._crossref_query` (paper left
  unstamped, batch stopped) and `fetch_oa._query_crossref_pdf_links`.
  The on-demand/ingest-time Crossref lookups are gated too (ruling 2):
  `ingest/crossref.lookup_crossref` (the `crossref_fn` default of
  `doi_backfill` and `metadata_resolve`; a refusal raises, never a
  "not found"), `ingest/provenance` (DOI check → `check_failed`, candidate
  search → `[]`, DOI validity → unstamped), `doi_registry._crossref_meta`
  (→ `RegistryLookupError`), `crossref_citation` (→ `[]`) and
  `si_discovery` (→ the existing transport miss). No HTTP is sent on a
  refusal. `bib_parse.run_bib_rematch_sweep` (ruling 3) re-matches entries
  a Crossref query failure left `match_conf IS NULL` on a stamped paper —
  never a true no-match (0.0) — at most 50 per run, only on a cycle whose
  claim pass found nothing, stopping on the first limiter refusal.
- **Open:** an entry Crossref can never answer (a 400 on odd citation
  text, an unparseable body) stays NULL and the sweep may re-pick it on
  every idle cycle — add a per-entry attempt marker if that shows up in
  the limiter budget. The crossref seed row is 20 rps; ruling 1 lowers it to 1/s in a
  new migration.

## Rulings (Reto, 2026-10-16 ~10:2xZ, morning-queue entry 7)

1. Crossref gets at most 1 request/s: "Be nice. We have time." The cap is
   1/s whatever Crossref's headers say. Header reading, one polite GET
   (`/works?rows=0` with the project mailto) at 2026-10-16 10:25:37Z:
   `x-rate-limit-limit: 3`, `x-rate-limit-interval: 1s`,
   `x-concurrency-limit: 3`, `x-api-pool: polite-array`.
2. Gate the on-demand and ingest-time Crossref lookups (`doi_backfill`,
   `metadata_resolve`, `provenance`, `doi_registry`, `crossref_citation`,
   `si_discovery`) through the same `require()` chokepoint.
3. Re-match `bib_parse` entries left NULL-match by a plain Crossref failure
   (rate-limit or transport error, not a true no-match), at the limiter's
   pace; idempotent and bounded per run.

## Reference assets

- EDGAR's in-process `TokenBucket` (`handlers/_edgar_client.py:98-129`) — ready
  algorithm reference for the rate lane.
- `resource_slots` (`store/_resource_slots_ops.py`) — the LLM-backend analogue
  of the cross-host coordination pattern.
