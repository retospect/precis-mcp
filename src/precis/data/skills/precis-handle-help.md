---
id: precis-handle-help
family: tools
title: precis — spoken word handles for ids
summary: convert a numeric ref id to three speakable words and back, for voice use
answers:
  - how do I say a six-digit gripe or ref id aloud?
  - how do I turn spoken words like "amber brave dog" back into an id?
applies-to: get(kind='handle')
kinds: [handle]
tags: orientation
status: active
---

# precis-handle-help — say an id in words

`handle` converts numeric ids and word handles locally. It also looks up
references by spoken description when you supply a reference kind.

## Say a numeric id in words
## Number to words

```python
get(kind="handle", id=648264)
```

Returns `648264 = <colour> <adjective> <noun>`: three common, homophone-free
words. The map is deterministic and exactly reversible; no two ids share
words (ids above 2,097,151 simply gain leading words).

## Turn spoken words back into an id
## Words to number

```python
get(kind="handle", q="amber brave dog")
```

Returns the number. Case, spaces, hyphens and commas are ignored. Words off
the lists, or the wrong word count, raise `BadInput`. Use the resulting
number as `id=` in `get`/`put`/`tag`; those verbs do not take the words.

## Recover a reference from a spoken description

```python
get(kind="handle", q="the Miller paper", args={"kind": "paper"})
```

Choose the matching reference from up to five candidates, shown as
`handle | spoken name | summary`. The supported kinds are paper, gripe,
todo and memory. Include the year or topic when several candidates fit.

## See also

- [[precis-overview]] — verbs and kinds
- [[precis-word-handles-help]] — shared voice and transcript conventions
- [[precis-calc-help]] — other local conversions
