# News ops — feed registry, poller internals, briefing delivery

When you need to add/manage RSS feeds, debug the `news_poll` or `briefing`
worker passes by hand, or understand the dedup/backoff/delivery mechanics
behind `precis-news-help`.

## Ingestion: the `news_poll` worker + `news_sources` registry

Feeds live in the operator-editable `news_sources` table (one row per
feed). The `news_poll` pass walks every enabled row, parses each feed
(feedparser), and mints any new article as a `news` ref. By default the
article **body comes straight from the feed entry** (`content`/`summary`,
HTML-stripped) — feedparser only, no page fetch. **No duplicate stories:**
each item is deduped on the feed's `<guid>` (the outlet's stable per-story
id, source-scoped — so a story re-posted under a changed URL isn't taken
twice) and on the canonical URL. **Polite polling:** a conditional GET
(`etag`/`last-modified`) means an unchanged feed returns `304` and isn't
re-downloaded. Article pages are never fetched at all (RSS-only).

Run one pass by hand:

```
precis worker --only news_poll --once
```

Managing feeds (plain SQL against the registry):

```sql
-- add a feed
INSERT INTO news_sources (url, title, source_slug, category, default_tags)
VALUES ('https://example.com/rss', 'Example', 'example', 'tech', '{topic:tech}');
-- park a feed without deleting it
UPDATE news_sources SET enabled = false WHERE source_slug = 'example';
```

**Failing-feed backoff:** a source that errors is retried on an
exponential backoff (`30min · 2^(N-1)`, capped ~1 day) keyed off
`consecutive_errors` — it stops being hammered every tick and self-heals
once it recovers. `last_status` / `consecutive_errors` on the row show
the state.

## On-demand article fetch

On-demand page fetch (`get(kind='news', id='<url>')`) uses trafilatura/httpx
(core deps, always present). The scheduled poller doesn't fetch pages — it
ingests straight from the feed.

## The morning briefing

The `briefing` pass summarizes recent `news` refs (last ~26h) via the
litellm `summarizer` alias and persists a dated, searchable
`briefing-<date>` ref. Optionally it **delivers** the brief by queuing a
`message` ref (`put(kind='message', target=…)`) — asa_bot, the one process
holding a Discord socket, posts it verbatim. The worker needs no socket;
delivery is just a DB write, idempotent per brief-date.

```
precis worker --only briefing --once
```

## Lineage

Replaces the retired `daily_briefing` / `rss_ingest` monolith stack: a
news item is now a queryable ref instead of a row in a bespoke
`news_items` table, and the briefing reads `news` refs back out.
