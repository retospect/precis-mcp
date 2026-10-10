---
id: precis-news-help
family: tools
title: precis — news kind (RSS ingestion + morning briefing)
summary: multi-source news as first-class refs; news_poll feed ingestion, news_sources registry, subreddit/Mastodon sources via put, search/tag, and the scheduled morning briefing with delivery
answers:
  - how do I read today's news items?
  - how does the news_poll worker ingest new sources?
  - how do I follow a subreddit or a Mastodon account as news?
  - how do I get a morning news briefing scheduled?
applies-to: get/put/search (kind='news'); precis worker --only news_poll|briefing; news_sources table; recurring-todo scheduling
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

New articles arrive on a schedule from the `news_sources` feed registry.
There's no `put` for minting a single news ref; arbitrary RSS/Atom feeds are
added by a human operator (`docs/runbooks/news-ops.md`).

### Subreddits and Mastodon accounts as sources

Both expose public, credential-free RSS, so you can register them yourself;
`put` stores the resolved feed URL as a `news_sources` row and the next
`news_poll` tick ingests it (nothing is fetched at put time):

```python
put(kind="news", text="reddit:r/python")  # → https://www.reddit.com/r/python/.rss
put(kind="news", text="mastodon:Gargron@mastodon.social")  # → https://mastodon.social/@Gargron.rss
put(kind="news", text="reddit:r/python", title="Python subreddit", tags=["topic:python"])
```

Only these two forms are accepted. `tags=` become the row's `default_tags`
(stamped on every article from it); `title=` overrides the label. Articles
are tagged `source:reddit-<name>` / `source:mastodon-<user>-<instance>`
(dots → dashes), so `search(kind="news", tags=["source:reddit-python"])`
scopes to one source. Re-registering an existing source is a no-op (it
re-enables a parked row). A mistyped name isn't caught at put: the row's
`last_status` shows the fetch error after the first poll and the feed backs
off. Posts without a title (Mastodon) take their first line as the title;
feed text passes the same tier-0 injection scan as every other feed.

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
