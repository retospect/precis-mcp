---
status: in-progress
title: Novel-ideas feed — arXiv RSS + Hacker News digest, dark
pillar: memory-graph
prio: low
---

# Novel-ideas feed — arXiv RSS + Hacker News digest, dark

Closes gr543714's needs-spec (Reto picked this spec 2026-11-01):
youtube recommendations were noise; the replacement is a daily digest of
novel ideas judged against what the system actually builds toward.

## Motivation / why

A bounded stream of outside ideas worth one morning glance — preprints
and Show-HN-style launches scored for novelty and relevance against the
roadmap pillars and the open quests — instead of engagement-ranked video
recommendations.

## In scope

- Sources: arXiv listing RSS (`rss.arxiv.org/rss/<cat>`) over a
  configured category list matching the pillars
  (`idea_feed.arxiv_categories`, default cs.AI/cs.CL/cs.IR/cs.LG +
  cond-mat.mes-hall/physics.chem-ph), and Hacker News top + show via the
  official Firebase API (`hacker-news.firebaseio.com/v0`).
- Judge: one batch LLM call per run on the smallest router tier,
  registered as the `idea_feed` llm.op entry (SMALL default, steerable
  via `llm.op.idea_feed`). Scores novelty + relevance against the
  pillar summary and the live `STATUS:active` quest titles.
- Cost: hard cap $1/UTC-day on the judge (`idea_feed.daily_usd`), summed
  from `llm_call_log` rows with `source='idea_feed'` — the per-op daily
  cap shape of `llm.quota_fallback_daily_usd`. Unreadable accounting
  fails closed (no judge call). Landing hits are free.
- Landing: one `kind='todo'` digest per run listing the top hits with
  links and a one-line why each — not one todo per hit.
- Cadence: daily `idea_feed` scheduler cadence (lease = fleet-singleton
  throttle); `spends=True` so the global budget breaker also gates it.
- Dedupe: against already-ingested refs by arXiv id / DOI / canonical
  URL, and against previous digests via a capped `idea_feed:seen`
  app_state map (60-day retention).
- HTTP: every fetch through `safe_get` inside `with_backoff`
  (`arxiv` policy for the feeds; the host-row lane for the Firebase
  API), polite user agent, bounded timeouts. Tests use recorded
  fixtures + fakes; no network.
- **Ships dark:** no-ops until a `service_config` row lights
  `idea_feed` (`prio > 0`) — `precis service prio '*' idea_feed 1`;
  one-off run `precis worker --only idea_feed --once`.

## Explicitly NOT in scope

- No ingestion of the hits themselves — the digest names links; nothing
  mints news/web/paper refs.
- No Mastodon/blogs — the picked source set is arXiv + HN.
- No per-hit todos, no alerts; a quiet day mints nothing.

## Open questions / decisions log

- Judge threshold (score >= 0.55) and digest cap (10 lines) are code
  constants; tune after a week of digests.
- More sources (lab blogs, curated lists) is a follow-up once the
  digest proves useful.
