---
id: precis-youtube-help
title: precis — YouTube transcripts
applies-to: get (kind='youtube')
status: active
---

# precis-youtube-help — fetch YouTube transcripts

`youtube` returns the transcript text of a YouTube video. Free, no
API key, no quotas (within YouTube's rate limits). Cached for 30 days.

## Fetch a video transcript
## Get the captions for a YouTube video
## I want to read what was said in this video

```python
get(kind='youtube', id='dQw4w9WgXcQ')                    # 11-char id
get(kind='youtube', id='https://youtu.be/dQw4w9WgXcQ')   # any URL form
```

Response body is the transcript text; a `Watch: https://…` deep-link
is appended after the attribution footer.

## What does a youtube id look like?
## Accepted id formats for youtube
## Can I paste a YouTube URL directly?

The id is the 11-character video ID (`dQw4w9WgXcQ`). You can also
paste any YouTube URL form — the handler extracts the ID:

```python
get(kind='youtube', id='dQw4w9WgXcQ')
get(kind='youtube', id='https://www.youtube.com/watch?v=dQw4w9WgXcQ')
get(kind='youtube', id='https://youtu.be/dQw4w9WgXcQ')
get(kind='youtube', id='https://www.youtube.com/shorts/dQw4w9WgXcQ')
get(kind='youtube', id='https://www.youtube.com/embed/dQw4w9WgXcQ')
```

All forms collapse to the same cache row keyed on the video ID.

## Fetch in a specific language

Default is English (`en`). The `languages=` param rides inside the
top-level `args=` dict (the tool surface is `kind/id/view/q/args`):

```python
get(kind='youtube', id='dQw4w9WgXcQ', args={'languages': 'de'})
get(kind='youtube', id='dQw4w9WgXcQ', args={'languages': 'en,es'})  # fallback list
```

Different language requests cache separately — a German fetch doesn't
invalidate the English one.

## See what languages a video has

```python
get(kind='youtube', id='dQw4w9WgXcQ', view='languages')
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

## See also

```python
get(kind='skill', id='precis-overview')   # verbs and kinds
get(kind='skill', id='precis-cache')      # TTL and freshness
get(kind='skill', id='precis-web-help')   # fetch arbitrary URLs
```
