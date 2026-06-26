---
id: precis-oracle-help
title: precis — consult an oracle for a perspective shift
summary: wisdom-tradition consultation — stoic, zen, iching collections; random or numbered entries
applies-to: get/search/tag/link (kind='oracle')
status: active
---

# precis-oracle-help — consult an oracle

Oracles are curated wisdom-tradition collections in the store
(`stoic`, `zen`, `iching`, …). The canonical address is the tradition's
**record handle** `or<id>` (e.g. `or7`) — list the traditions to read it
off, then paste it back. Each tradition holds numbered entries you pull
with `or<id>~N` (or at random). The legacy slug (`stoic`) and the
`stoic~N` selector still resolve on input.

## List the available oracle traditions
## See what oracles I can consult
## What wisdom traditions are loaded?

```python
get(kind='oracle')                              # list all traditions live in this build
```

The list is build-specific — call it to see what's available. Each row
leads with the tradition's `or<id>` handle; copy it for the calls below.

## Consult an oracle for a random principle
## Pull one entry from a tradition
## Get a random Stoic / Zen / I-Ching passage

```python
get(id='or7')                                   # one random entry, by handle
get(kind='oracle', id='stoic')                  # legacy slug still resolves
get(kind='oracle', id='zen')
get(kind='oracle', id='iching')
```

Calling a tradition with no selector returns one entry chosen
at random (~150 tokens). Use this to perturb a stuck deliberation:
read the entry, notice your reaction, treat the reaction as the
signal.

## See every entry in a tradition
## Browse a tradition's full catalogue
## What entries does the I-Ching contain?

```python
get(id='or7', view='index')                     # titled catalogue, by handle
get(id='or7/index')                             # path form
get(kind='oracle', id='iching/index')           # legacy slug path → 64 hexagrams
```

Rows are drillable: paste the entry selector (`or7~N`, or the legacy
`slug~N`) as `id=`.

## Fetch a specific oracle entry
## Read entry N of a tradition deterministically
## Get I-Ching hexagram 49

```python
get(id='or7~4')                                 # entry 4 of stoic (or7), by handle
get(kind='oracle', id='stoic~4')                # legacy slug~N still resolves
get(kind='oracle', id='iching~49')              # Hexagram 49
```

Positions are 1-indexed; I-Ching numbers match the standard hexagram
sequence. The response trailer carries prev/next/index hops.

## Search across or within traditions
## Find an oracle entry matching a theme
## Which passage talks about impermanence?

```python
search(kind='oracle', q='impermanence')                       # all traditions
search(kind='oracle', q='timely change', scope='iching')      # within one
```

Title-lexical only — oracle entries carry no embeddings, so semantic
search does not apply and a query may return nothing. For a reliable
pick, address an entry directly with `get(id='or<id>~N')` (legacy
`get(kind='oracle', id='<tradition>~N')` still resolves). Each result
leads with the tradition's `or<id>` handle; order is the relevance
signal.

## Cite an oracle entry in writing
## Quote a passage with provenance
## Record that this principle informed a decision

```python
get(id='or7~9')                                 # fetch the entry text, by handle
put(kind='citation', text='The impediment to action advances action.',
    source_handle='or7~9',
    source_quote='The impediment to action advances action.',
    link='me88', rel='supports')               # legacy memory:88 still resolves
```

Cite by the entry handle `or<id>~N` (the legacy `oracle:<slug>~N` still
resolves). The entry title and tradition name come back in the response
header.

## Link or tag an oracle entry
## Annotate an oracle with a topic or relationship
## Mark this passage as the one that decided X

```python
tag(kind='oracle', id='or7', add=['topic:decision-aid'])
link(kind='oracle', id='or7',
     target='me88', rel='supports')             # legacy memory:88 still resolves
```

Open tags (`topic:*`, `project:*`) work freely. Tradition bodies are
curated; `put(kind='oracle', …)` is not exposed.

## When to reach for an oracle

- Analysis paralysis — two options balance; pull one entry, watch
  for the flinch.
- Stuck loop — same reasoning running in circles; a random reframe
  unsticks it.
- Pre-commit check — pull before shipping; if the principle
  contradicts the choice, take that seriously.
- Session warm-up — cheap starting angle when the agent has no
  context yet.

The oracle perturbs; the decision is still yours.

## See also

```python
get(kind='skill', id='precis-random-help')      # random pick from the whole corpus
get(kind='skill', id='precis-citation-help')    # quote oracle entries with provenance
get(kind='skill', id='precis-memory-help')      # log the decision an oracle helped make
get(kind='skill', id='precis-overview')         # verbs and kinds
```
