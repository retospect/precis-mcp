---
id: precis-put-help
title: precis — the put verb (create, annotate, import)
applies-to: put (every kind that supports it)
status: active
---

# precis-put-help — create a new ref

`put` is the create verb. It mints new refs and attaches tags or
links on the same call. Rewriting an existing ref's body lives on
`edit`; flipping tags or links on an existing ref lives on `tag`
and `link`.

## Jot a thought into memory
## Capture a quick note I want to keep
## How do I record something for later?

```python
put(kind='memory', text='Wang20 cites our 2024 result indirectly.')
put(kind='memory',
    text='Schedule the next sortie review for 2026-Q3.',
    tags=['topic-sortie', 'pinned'])
put(kind='memory',
    text='Heterojunction comparison aligns with our hypothesis.',
    link='paper:wang2020state', rel='cites')
```

Omit `id=` to mint a fresh numeric ref. The response returns the
new id; paste it into `get(kind='memory', id=<N>)` to read back.

## File a todo
## Add a task to my list
## I need to remember to do X

```python
put(kind='todo', text='Review section 3 of abazari2024design.')
put(kind='todo',
    text='Sweep open patents for cpc B01J27.',
    tags=['PRIO:high', 'project:noxrr'])
```

`STATUS:open` is the implicit default; flip with
`tag(kind='todo', id=<N>, add=['STATUS:done'])` once the task lands.

## Log a gripe
## Capture an annoyance to fix later
## How do I record a niggle?

```python
put(kind='gripe', text='Search misses on exact CPC codes — needs a lexical floor.')
put(kind='gripe',
    text='TOC excerpts feel too short on very long segments.',
    tags=['area:search'])
```

Gripes feed the retrieval-misses channel; one sentence per gripe.

## Stash a flashcard
## Create a spaced-repetition card
## How do I queue something to review?

```python
put(kind='fc',
    text='Q: What does the bare-DOI form of get(kind=paper) do?\nA: Resolves via metadata, fetches the paper.')
put(kind='fc',
    text='Q: ...\nA: ...',
    tags=['topic:precis'])
```

Card scheduling follows the SM-2 cadence — see `precis-fc-help`.

## Open a long-running quest
## Start a multi-step goal I'll come back to
## How do I track a project across sessions?

```python
put(kind='quest', text='Ship the v2 search reranker.')
put(kind='quest',
    text='Ingest the 2024 NOxRR corpus end-to-end.',
    tags=['project:noxrr'])
```

Link memories and todos into the quest with
`link(kind='memory', id=<N>, target='quest:<slug>', rel='part-of')`.

## Record a verified citation
## Stamp a claim with a source quote
## How do I write the verifier output back?

```python
put(kind='citation',
    text='Z-scheme NOxRR achieves 78% selectivity at 1.2 V.',
    source_handle='wang2020state~38..42',
    source_quote='...selectivity reached 78% at 1.2 V vs RHE...',
    verifier_confidence=0.92,
    link='paper:wang2020state', rel='cites')
```

Citation is the verifier's output kind. See `precis-citation-help`
for the verifier loop and the named-kwarg shape.

## Create a markdown file under PRECIS_ROOT
## Drop a new note file on disk
## How do I make a new .md file?

```python
put(kind='markdown', mode='create',
    id='notes/proj-fbproj.md',
    text='# fbproj — project notes\n\n## Goals\n- ...\n')
```

`mode='create'` is required for file kinds and is the only accepted
mode on `put`. `id=` is the path under `PRECIS_ROOT`. Append, insert,
replace, and find-replace live on `edit`.

## Create a plaintext, tex, or python file
## Drop a new .txt, .tex, or source file
## Same shape, different kind

```python
put(kind='plaintext', mode='create',
    id='logs/2026-06-05.txt', text='...')
put(kind='tex', mode='create',
    id='chapters/intro.tex', text='\\section{Intro}\n...')
put(kind='python', mode='create',
    id='precis::precis.utils.demo', text='def demo():\n    ...')
```

Same `mode='create'` discipline as `markdown`. The `id=` form for
`python` is `<repo>::<dotted.path>`; see `precis-python-help`.

## Import a Perplexity report I already paid for
## Stash a web-UI Sonar answer at $0
## How do I avoid double-paying for a Perplexity query I already ran?

```python
put(kind='websearch', mode='import',
    q='latest perovskite tandem efficiencies',
    text='<paste the answer body>')
put(kind='think', mode='import',
    q='compare DAC and BECCS', text='...')
put(kind='research', mode='import',
    q='mechanism of NOxRR', text='...')
```

`mode='import'` lands the report in the cache keyed on `q=`; the
next `get(kind='websearch', q='...')` hits cache at $0. See
`precis-perplexity-help`.

## Tag a ref while creating it
## Attach metadata on the same call
## How do I avoid a second tag() call?

```python
put(kind='memory', text='...', tags=['pinned', 'topic-sortie'])

put(kind='todo', text='...', tags=['PRIO:high'])
```

`tags=` runs only at creation. For retroactive tag changes use
`tag(kind='...', id=<N>, add=[...], remove=[...])`. Closed-prefix
axes (`STATUS:`, `PRIO:`, `SRC:`, `CACHE:`) are kind-gated; open
tags are universal. See `precis-tag-help`.

## Link a ref while creating it
## Attach an outbound edge on the same call
## How do I cite a paper from a new memory?

```python
put(kind='memory', text='Anchors our claim.',
    link='paper:wang2020state', rel='cites')
put(kind='memory', text='Touches the same idea.',
    link='paper:wang2020state~38..42', rel='discusses')
```

`link=` takes a single target `kind:identifier[~selector]` and `rel=`
defaults to `related-to`. For retroactive link changes use the `link`
verb. See `precis-link-help` for the relation vocabulary.

## Annotate a patent I can't put-create
## Attach a note to a read-only ref
## How do I add commentary to a patent?

Patents are read-only via OPS — `put(kind='patent', ...)` is
rejected. Hang a memory off the patent instead:

```python
put(kind='memory',
    text='Verification batch — see patent.',
    link='patent:ep4123456a1', rel='annotates')
```

Same trick works for any read-only kind.

## See also

```python
get(kind='skill', id='precis-overview')         # seven verbs, address grammar
get(kind='skill', id='precis-edit-help')        # sub-region rewrites of existing refs
get(kind='skill', id='precis-delete-help')      # soft-delete numeric refs, region delete on files
get(kind='skill', id='precis-tag-help')         # tag vocabulary and axis gating
get(kind='skill', id='precis-link-help')        # relation vocabulary
get(kind='skill', id='precis-citation-help')    # verifier-workflow citation shape
get(kind='skill', id='precis-perplexity-help')  # mode='import' for paid kinds
get(kind='skill', id='precis-files-help')       # file-kind addressing
```
