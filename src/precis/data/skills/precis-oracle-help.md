---
id: precis-oracle-help
title: precis — consult an oracle for a perspective shift
applies-to: get/search/tag/link (kind='oracle')
status: active
---

# precis-oracle-help — consult an oracle

Oracles are curated wisdom-tradition collections in the store
(`stoic`, `zen`, `iching`, …). Address by slug; each tradition holds
numbered entries you can pull individually or at random.

## List the available oracle traditions
## See what oracles I can consult
## What wisdom traditions are loaded?

```python
get(kind='oracle')                              # list all traditions live in this build
```

The list is build-specific — call it to see what's available.

## Consult an oracle for a random principle
## Pull one entry from a tradition
## Get a random Stoic / Zen / I-Ching passage

```python
get(kind='oracle', id='stoic')                  # one random entry
get(kind='oracle', id='zen')
get(kind='oracle', id='iching')
```

Calling a tradition slug with no selector returns one entry chosen
at random (~150 tokens). Use this to perturb a stuck deliberation:
read the entry, notice your reaction, treat the reaction as the
signal.

## See every entry in a tradition
## Browse a tradition's full catalogue
## What entries does the I-Ching contain?

```python
get(kind='oracle', id='<slug>', view='index')   # titled catalogue
get(kind='oracle', id='<slug>/index')           # path form
get(kind='oracle', id='iching/index')           # 64 hexagrams
```

Rows are drillable: paste the handle (`slug~N`) as `id=`.

## Fetch a specific oracle entry
## Read entry N of a tradition deterministically
## Get I-Ching hexagram 49

```python
get(kind='oracle', id='stoic~4')                # entry 4 of stoic
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

Hybrid lexical + semantic. Results are oracle handles; order is the
relevance signal.

## Cite an oracle entry in writing
## Quote a passage with provenance
## Record that this principle informed a decision

```python
get(kind='oracle', id='stoic~9')                # fetch the entry text
put(kind='citation', text='The impediment to action advances action.',
    source_handle='oracle:stoic~9',
    source_quote='The impediment to action advances action.',
    link='memory:88', rel='supports')
```

Cite as `oracle:<slug>~N`. The entry title and tradition name come
back in the response header.

## Link or tag an oracle entry
## Annotate an oracle with a topic or relationship
## Mark this passage as the one that decided X

```python
tag(kind='oracle', id='stoic~9', add=['topic:decision-aid'])
link(kind='oracle', id='stoic~9',
     target='memory:88', rel='supports')
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
