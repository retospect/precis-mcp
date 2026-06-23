---
id: precis-news-help
title: precis — news kind (RSS ingestion + morning briefing)
summary: multi-source news as first-class refs; news_poll feed ingestion, news_sources registry, search/tag, and the scheduled morning briefing with delivery
applies-to: get/search (kind='news'); precis worker --only news_poll|briefing; news_sources table; recurring-todo scheduling
status: active
---

# precis-news-help — news in the corpus

A `news` ref is a single news article: URL-addressed, pinned in cache,
its body block-split + embedded like `web`/`wikipedia`, so
`search(kind='news', q=...)` lands hits inside article text. News is a
first-class kind, not a side table — it searches, tags, and links like
everything else. Every article is stamped `category:news` +
`source:<slug>` (plus the feed's `default_tags` and a `published:<date>`
tag), so you filter it in or out of search by tag.

## Reading news

```python
get(kind='news')                          # list ingested articles
search(kind='news', q='semiconductors')   # search inside article bodies
search(q='...', tags=['source:bbc'])      # scope to one source
search(q='...', tags=['category:news'])   # news only, across the corpus
```

Each ingested article has a handle — `nw<id>` (the 2-char `news` code +
its decimal ref id, computed not stored; `nc<id>` for a body chunk), ADR
0036. The handle is its stable address; the source URL is **metadata, not
the address** (kept on the ref for dedup / re-fetch / citation). Search
output shows the handle; copy it back into `get`, no `kind=` needed:

```python
get(id='nw42')               # handle infers kind=news; see precis-addressing-help
```

A single article on demand by URL (fetches + extracts the live page):

```python
get(kind='news', id='https://www.bbc.com/news/articles/abc123')
```

On-demand page fetch needs the `[external]` extra (trafilatura/httpx). The
scheduled poller does **not** — it ingests straight from the feed.

## Ingestion: the `news_poll` worker + `news_sources` registry

Feeds live in the operator-editable `news_sources` table (one row per
feed). The `news_poll` pass walks every enabled row, parses each feed
(feedparser), and mints any new article as a `news` ref. By default the
article **body comes straight from the feed entry** (`content`/`summary`,
HTML-stripped) — feedparser only, no page fetch. Dedup is by canonical
URL, so re-polls are cheap and idempotent.

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

Read a past brief by its handle (`get(id='nw<id>')`) or, as a legacy
convenience, by its dated slug `get(kind='news', id='briefing-2026-06-23')`.

## Scheduling (recurring todos, not OS timers)

Both passes run on the always-on system worker via recurring todos — no
launchd/cron job. The schedule + dispatch passes tick them; the
`news_poll` / `briefing` job_types run the work in-process (see
`precis-recurring-help` for the recurring-todo mechanics).

```python
# poll feeds every 30 minutes
put(kind='todo', text='news poll', tags=['level:recurring'],
    meta={'schedule': {'every': '30m'},
          'executor': 'claude_inproc', 'job_type': 'news_poll', 'params': {}})

# brief every morning at 07:00 UTC, delivered to a Discord channel
put(kind='todo', text='morning briefing', tags=['level:recurring'],
    meta={'schedule': {'cron': '0 7 * * *'},
          'executor': 'claude_inproc', 'job_type': 'briefing',
          'params': {'deliver_to': 'discord/<guild>/<channel>/<channel>'}})
```

Omit `params.deliver_to` to only persist the brief without delivering it.

## Lineage

Replaces the retired `daily_briefing` / `rss_ingest` monolith stack: a
news item is now a queryable ref instead of a row in a bespoke
`news_items` table, and the briefing reads `news` refs back out.
