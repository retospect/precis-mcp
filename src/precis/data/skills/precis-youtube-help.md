---
id: precis-youtube-help
title: precis — YouTube transcripts
status: phase-4
tier: 1
floor: any
applies-to: get (kind='youtube')
last-updated: 2026-04-26
---

# precis-youtube-help — fetch video transcripts

`youtube` returns the transcript text of a YouTube video. Free, no
API key, no quotas (within YouTube's rate limits).

```python
get(kind='youtube', id='dQw4w9WgXcQ')                    # bare id
get(kind='youtube', id='https://youtu.be/dQw4w9WgXcQ')   # short URL
get(kind='youtube', id='https://www.youtube.com/watch?v=dQw4w9WgXcQ')
get(kind='youtube', id='https://www.youtube.com/shorts/dQw4w9WgXcQ')
```

All URL forms (watch, youtu.be, shorts, embed, live, mobile) collapse
to the same cache row keyed on the 11-character video ID.

## Languages

Default is English (`en`). To request a specific language:

```python
get(kind='youtube', id='dQw4w9WgXcQ', languages='de')
get(kind='youtube', id='dQw4w9WgXcQ', languages='en,es')   # fallback list
```

To see what's available before committing to a fetch:

```python
get(kind='youtube', id='dQw4w9WgXcQ', view='languages')
```

Different languages cache separately — a German fetch doesn't
invalidate the English one.

## Caching

Transcripts rarely change after upload, so cache TTL is **30 days**.
Fetches and cache hits are both `[cost: free]`. The agent can force a
re-fetch by deleting the ref (phase 5+; for now wait for TTL or change
language).

## Common errors

- `NotFound: transcripts are disabled` — the uploader disabled them.
  No automatic transcripts available.
- `NotFound: no transcript for X in languages=[...]` — the requested
  language doesn't exist. Run `view='languages'` first to see what's
  there.
- `NotFound: video X is unavailable` — private, deleted, or
  region-locked.

## See also

- `precis-overview` — verbs and kinds
- `precis-cache` — TTL and freshness semantics
