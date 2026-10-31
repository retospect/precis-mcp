---
id: precis-youtube-help
family: tools
title: precis — YouTube transcripts
summary: YouTube transcript fetch — captions by video id or URL, 30-day cache, free. ALWAYS use this for a YouTube URL — never a native youtube_transcript_api / web-fetch.
answers:
  - how do I fetch the transcript for a YouTube video?
  - can I paste a YouTube URL directly instead of the bare video id?
  - how do I fetch a transcript in a specific language?
  - what happens when a video has no transcript?
  - how do I read a transcript too long for one result?
  - how do I list the YouTube videos already cached?
  - how do I find a cached YouTube video from a topic or channel?
applies-to: get (kind='youtube')
tags: [external-sources]
kinds: [youtube]
status: active
---

# precis-youtube-help — fetch YouTube transcripts

`youtube` returns the transcript text of a YouTube video. Free, no
API key. Cached for 30 days.

**Safe rate:** YouTube rate-limits bursts. Serialise fetches — one
`get` at a time, never a parallel fan-out. A `RateLimited` refusal
means back off a few minutes, then retry the same call.

> **Routing rule — read first.** For **any** YouTube URL or video id,
> reach for `get(kind='youtube', …)` **before** any native tool. Do **not**
> call `youtube_transcript_api`, a shell `yt-dlp`, or a generic web fetch —
> those bypass the 30-day cache, the language handling, and the attribution
> footer, and (as seen in the field) simply fail, sending you down a slower
> researcher-agent fallback. The general principle: **before reaching for a
> native library, check whether precis already has a kind for it**
> (`search(kind='skill', q='<the thing>')`) — YouTube, arbitrary web pages
> (`kind='web'`), Wikipedia, and web search all have one.

## Fetch a video transcript
## Get the captions for a YouTube video
## I want to read what was said in this video

```python
get(kind="youtube", id="dQw4w9WgXcQ")  # 11-char id
get(kind="youtube", id="https://youtu.be/dQw4w9WgXcQ")  # any URL form
```

Response body is the transcript text; a `Watch: https://…` deep-link
is appended after the attribution footer.

## What does a youtube id look like?
## Accepted id formats for youtube
## Can I paste a YouTube URL directly?

The id is the 11-character video ID (`dQw4w9WgXcQ`). You can also
paste any YouTube URL form — the handler extracts the ID:

```python
get(kind="youtube", id="dQw4w9WgXcQ")
get(kind="youtube", id="https://www.youtube.com/watch?v=dQw4w9WgXcQ")
get(kind="youtube", id="https://youtu.be/dQw4w9WgXcQ")
get(kind="youtube", id="https://www.youtube.com/shorts/dQw4w9WgXcQ")
get(kind="youtube", id="https://www.youtube.com/embed/dQw4w9WgXcQ")
```

All forms collapse to the same cache row keyed on the video ID.

## Fetch in a specific language

Default is English (`en`). The `languages=` param rides inside the
top-level `args=` dict (the tool surface is `kind/id/view/q/args`):

```python
get(kind="youtube", id="dQw4w9WgXcQ", args={"languages": "de"})
get(kind="youtube", id="dQw4w9WgXcQ", args={"languages": "en,es"})  # fallback list
```

Different language requests cache separately — a German fetch doesn't
invalidate the English one.

## Read a long transcript
## The transcript is too long for one result

Long transcripts are stored in time-windowed chunks (~1-2 kB, each with
its start/end time) and read back in ranges — a whole-body `get` over
the 24 kB transport cap returns the head plus a chunk outline, and its
`next:` line names the first range call. `view='toc'` maps every chunk
with its time window and size:

```python
get(kind="youtube", id="dQw4w9WgXcQ", view="toc")  # chunk map
get(kind="youtube", id="dQw4w9WgXcQ~0..3")   # first chunk range
get(kind="youtube", id="dQw4w9WgXcQ~4..7")   # next page
get(kind="youtube", id="dQw4w9WgXcQ~2")      # one chunk only
```

Each page fits the result budget; the render header says how many
chunks the video holds (`4 of 70 chunks`) and the `Next:` trailer
names the following range, so you can walk the body end to end.

## Fetch many transcripts — the bulk path
## I have a list of YouTube videos to fetch

```python
put(kind="youtube", args={"op": "fetch_many", "ids": ["dQw4w9WgXcQ", "https://youtu.be/…", …]})
put(kind="youtube", args={"op": "fetch_many", "ids": […], "languages": "en,de"})
```

One call enqueues a single `youtube_bulk` job on the worker: it walks
the ids **serially** through the same cache path as a single `get` —
each lands cached exactly as if you had fetched it yourself. This is
the only supported way to fetch a list: **never** run N `get` calls in
parallel subagents — the burst trips YouTube's host block and every
caller fails together.

- Already-cached ids are **skipped**; re-submitting a finished list is
  a no-op for the done ids (a crashed or partial run resumes by
  resubmitting the same `put`).
- A throttle backs off ~60 s and retries the id instead of failing the
  job; an id that stays blocked is recorded `failed: rate_limited`.
- `get(kind='job', id=N)` shows progress (`meta.progress` tallies);
  the `job_result` chunk carries the per-id ok/failed/skipped map.

## Browse what's already cached
## List the cached YouTube videos

```python
get(kind="youtube", id="/recent")                    # page 1
get(kind="youtube", id="/recent", args={"page": 2})  # next page
```

One row per cached video: video id, title, channel, duration and
cache state, newest first. A bare `get(kind="youtube")` shows the
same listing. Read one without triggering a fetch:

```python
get(kind="youtube", id="dQw4w9WgXcQ", args={"no_fetch": True})
```

## Find a cached video from a topic or channel

```python
search(kind="youtube", q="neighbouring ions")   # title + channel + transcript
search(kind="youtube", q="Veritasium")          # channel names match too
```

Hits come back **grouped per video** (best transcript chunk per
video, one row each) — a long talk can't flood the page.
`per_paper=N` raises the chunks shown per video; `page_size=` sets how
many videos come back (there is no page 2 — refine `q=`). When nothing matches, the empty state lists the closest
cached titles instead of a dead end.

## See what languages a video has

```python
get(kind="youtube", id="dQw4w9WgXcQ", view="languages")
```

Lists every available transcript (manual + auto-generated). Use this
before committing to a fetch when you're unsure which codes work.

## When a transcript is missing

- `NotFound: transcripts are disabled` — the uploader turned them
  off. Nothing to fetch.
- `NotFound: no transcript for X in languages=[...]` — the requested
  language doesn't exist. Run `view='languages'` to see what does.
- `NotFound: video X is unavailable` — verify the URL; the video may
  be private, deleted, or region-blocked.
- `RateLimited: YouTube is rate-limiting this host` — YouTube blocked
  this host for too many requests. Wait a few minutes, retry the same
  call, and serialise fetches — parallel bursts are what trips the
  block.

## See also

- [[precis-overview]] — verbs and kinds
- [[precis-cache]] — TTL and freshness
- [[precis-web-help]] — fetch arbitrary URLs
