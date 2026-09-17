---
id: precis-news-help
title: precis — news kind (RSS ingestion + morning briefing)
summary: multi-source news as first-class refs; news_poll feed ingestion, news_sources registry, search/tag, and the scheduled morning briefing with delivery
answers:
  - how do I read today's news items?
  - how does the news_poll worker ingest new sources?
  - how do I get a morning news briefing scheduled?
applies-to: get/search (kind='news'); precis worker --only news_poll|briefing; news_sources table; recurring-todo scheduling
status: active
tags: [workflow, external-sources]
kinds: [news]
---

# precis-news-help — news in the corpus

A `news` ref is a single news article: URL-addressed, its body embedded and
searchable like `web`/`wikipedia`. Every article is stamped `category:news` +
`source:<slug>` (plus a `published:<date>` tag), so you filter it in or out
of search by tag.

## Reading news

```python
get(kind="news")  # list ingested articles
search(kind="news", q="semiconductors")  # search inside article bodies
search(q="...", tags=["source:bbc"])  # scope to one source
search(q="...", tags=["category:news"])  # news only, across the corpus
```

Each article has a handle (`nw<id>`); copy it back into `get`, no `kind=`
needed:

```python
get(id="nw42")  # handle infers kind=news; see precis-addressing-help
```

Fetch a single article on demand by URL:

```python
get(kind="news", id="https://www.bbc.com/news/articles/abc123")
```

## Ingestion

New articles arrive on a schedule from an operator-managed feed registry —
there's no `put` for minting a news ref from a feed. Ask a human operator to
add or manage one (`docs/runbooks/news-ops.md`).

## The morning briefing

A recurring pass summarizes recent news and persists a dated, searchable
brief; optionally it delivers into a Discord thread. Read a past brief by
its handle:

```python
get(id="nw<id>")
```

## Scheduling (recurring todos, not OS timers)

Both passes run via recurring todos (see `precis-recurring-help`):

```python
put(
    kind="todo",
    text="news poll",
    meta={
        "schedule": {"every": "30m"},
        "executor": "claude_inproc",
        "job_type": "news_poll",  # or "briefing", schedule={"cron": "0 7 * * *"}
        "params": {},  # briefing: {"deliver_to": "conv:discord/<g>/<c>/<t>"}
    },
)
```

A `conv:` delivery target mirrors into that thread's history, so follow-ups
see the brief as context. Omit `params.deliver_to` to persist without
delivering.
