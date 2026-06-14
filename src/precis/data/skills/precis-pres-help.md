---
id: precis-pres-help
title: precis — store and search slide decks + unpublished writeups
summary: internal artefacts — slide decks, unpublished writeups, course notes; subtype taxonomy
applies-to: get/search/put/tag/link (kind='pres')
status: active
---

# precis-pres-help — slide decks, drafts, course notes

`pres` is the kind for internal work the user wants searchable but
kept separate from the academic paper library (`paper`). Use it for:

- **Slide decks** (`subtype:slides`) — PDFs of talks, lectures,
  conference presentations.
- **Unpublished writeups** (`subtype:writeup`) — drafts, internal
  reports, working documents.
- **Course / talk notes** (`subtype:notes`) — note-form artefacts
  associated with a talk or class.

One ref per artefact. One block per slide (for decks) or per
paragraph (for writeups). Slug-addressed: pick a stable slug
(date + topic works well, e.g. `2026-06-talk-precis-architecture`).

## Add a new deck or writeup

## Ingest a slide deck one slide at a time
## Capture an unpublished writeup

```python
put(kind='pres',
    id='2026-06-talk-precis-architecture',
    text='Slide 1 body: title, author, date.',
    pos=0,
    title='Precis architecture (talk @ Cluster Demo Day)',
    subtype='slides',
    ref_meta={'venue': 'cluster demo day',
              'date': '2026-06-04',
              'audience': 'internal',
              'source_pdf': '/path/to/talk.pdf'})

# Subsequent slides — title and subtype are ignored on update.
put(kind='pres', id='2026-06-talk-precis-architecture',
    text='Slide 2 body: what is precis?', pos=1)
put(kind='pres', id='2026-06-talk-precis-architecture',
    text='Slide 3 body: 22 kinds…', pos=2)
```

Omit `pos=` to append at the next free position. Re-putting at an
existing `pos` overwrites that block (useful for fixing OCR noise
on a single slide without rebuilding the whole deck).

For writeups, pass `chunk_kind='paragraph'` so the renderer
labels blocks as paragraphs instead of slides:

```python
put(kind='pres',
    id='2026-06-draft-cluster-postmortem',
    text='The cluster went down at 03:14 on 2026-06-02 because…',
    pos=0,
    title='Cluster postmortem — 2026-06-02 outage',
    subtype='writeup',
    chunk_kind='paragraph',
    ref_meta={'authors': [{'family': 'Stamm'}],
              'date': '2026-06-08'})
```

## Read a deck

## Get the slide deck overview
## Show me all the slides

```python
get(kind='pres', id='2026-06-talk-precis-architecture')        # overview
get(kind='pres', id='2026-06-talk-precis-architecture/full')   # all blocks
get(kind='pres', id='2026-06-talk-precis-architecture~2')      # single block
```

## Browse what's been stored

```python
get(kind='pres')                  # 20 most recent
get(kind='pres', id='/recent')    # explicit
```

## Find a slide or paragraph

```python
search(kind='pres', q='hnsw index on chunk_embeddings')
search(kind='pres', q='vector', scope='2026-06-talk-precis-architecture')
```

Lexical search across all `pres` blocks. Cross-kind search
(`search(kind='*', q='...')`) folds `pres` hits in alongside paper /
memory matches when a question spans both.

## Link a deck back to the papers it cites
## Cross-reference a writeup with an internal decision

```python
link(kind='pres', id='2026-06-talk-precis-architecture',
     target='paper:vaswani2017attention', rel='cites')

link(kind='pres', id='2026-06-draft-cluster-postmortem',
     target='gripe:3712', rel='derived-from')
```

Open-tag axes only; no closed workflow vocabulary on `pres`. To
mark a draft's status, use a paired `todo` or `gripe`.

## Tags

```python
tag(kind='pres', id='2026-06-talk-precis-architecture',
    add=['topic:agent-architecture', 'audience:internal'])
```

## See also

```python
get(kind='skill', id='precis-overview')         # verbs and kinds
get(kind='skill', id='precis-search-help')      # search mechanics
get(kind='skill', id='precis-paper-help')       # academic papers (cite_key shape)
get(kind='skill', id='precis-relations')        # cites, derived-from, supports
```
