---
id: precis-handle-help
family: tools
title: precis — spoken word handles for ids
summary: convert a numeric ref id to speakable words and back, for voice use
answers:
  - how do I say a six-digit gripe or ref id aloud?
  - how do I turn spoken words like "amber brave dog" back into an id?
applies-to: get(kind='handle'); put(kind='handle')
kinds: [handle]
tags: orientation
status: active
---

# precis-handle-help — say an id in words

`handle` is a stateless converter. Call it only when you need to speak or
decode an id; rows and acks carry no word handles unless the session
switch is on (see [[precis-word-handles-help]]), so normal traffic pays
no tokens. Computed in code, free.
It also looks up references by spoken description when you supply a
reference kind.

## Say a numeric id in words
## Number to words

```python
get(kind="handle", id=648264)
```

Returns `648264 = <adjective> <noun>`: two common, homophone-free words
naming the id mod 16384. When another live ref shares the pair the
response is the three-word `<colour> <adjective> <noun>` form plus a
clash note — say all three to pick exactly one. `id=` also takes a list
and answers one `id = words` line per id.

## Turn spoken words back into an id
## Words to number

```python
get(kind="handle", q="amber brave dog")
```

Three words return the number; the map is deterministic and exactly
reversible. Two words (`q="brave dog"`) return the live refs sharing
that pair — handle, three-word form, kind, size, summary, newest first —
because a pair is not unique; a long list reports its true total and
pages with `args={'page': N}`. Case, spaces, hyphens and commas are
ignored. Words off the lists raise `BadInput`. Use the resulting number
as `id=` in `get`/`put`/`tag`; those verbs do not take the words.

## Turn word handles on for the session

```python
put(kind="handle", text="on")
```

Pins this session's switch: search and list rows gain a `word` column so
a voice agent can speak each row without a per-id lookup. Voice clients
may be auto-enabled by the deployment. See [[precis-word-handles-help]]
for when to say the words and how clashes resolve.

## Recover a reference from a spoken description

```python
get(kind="handle", q="the Miller paper", args={"kind": "paper"})
```

Choose the matching reference from up to five candidates, shown as
`handle | spoken name | summary`. The supported kinds are paper, gripe,
todo and memory. Include the year or topic when several candidates fit.

## See also

- [[precis-word-handles-help]] — voice conventions, the session switch, clashes
- [[precis-overview]] — verbs and kinds
- [[precis-calc-help]] — other local conversions
