---
status: in-progress
title: Shared cross-host rate limiter for outbound external APIs (keyed, two-lane)
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

- **v1 (shipping):** table + module (both lanes) + S2 rate wiring, flag-gated,
  fail-open. openalex/unpaywall/arxiv/crossref seeded as **dormant config rows**
  (both lanes provisioned in schema → no future migration to wire them).
- **Follow-on:** wire OpenAlex (rate + daily quota lane — first real quota-lane
  consumer), Unpaywall (daily), arXiv (rate), Crossref (rate, last). Derive
  limiter keys from the `uses_external=(…)` tuples already declared per pass in
  `workers/registry.py`.
  - **The quota lane only bites if the caller honors `acquire()`'s return.**
    v1 S2 call sites call `acquire("s2")` as a bare statement and proceed
    regardless — correct for a *rate* lane, where the bounded blocking wait
    *is* the coordination. But a daily-cap provider returns `False` when the
    quota is exhausted, and a caller that ignores it fires the request anyway,
    defeating the hard cap. So the OpenAlex/Unpaywall wiring **must**
    `if not acquire(provider): skip/defer` (not just call-and-proceed).

## Reference assets

- EDGAR's in-process `TokenBucket` (`handlers/_edgar_client.py:98-129`) — ready
  algorithm reference for the rate lane.
- `resource_slots` (`store/_resource_slots_ops.py`) — the LLM-backend analogue
  of the cross-host coordination pattern.
