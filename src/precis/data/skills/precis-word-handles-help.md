---
id: precis-word-handles-help
family: core
title: precis — spoken descriptions and word handles
summary: name references in voice chat and podcasts, speak two-word handles with the session switch, resolve clashes, and recover refs from spoken descriptions
answers:
  - how do I name references when talking by voice?
  - how do I recover a handle from a spoken name?
  - how do I turn word handles on for this session?
  - what happens when two refs share the same two words?
applies-to: voice chat, general agent chat, draft or podcast narration; get(kind='handle'); put(kind='handle')
tags: [addressing]
kinds: [handle]
status: active
---

# Spoken descriptions and word handles

For a quick start in a live conversation, read [[precis-voice-chat-help]].

## Talking by voice: name things, keep handles recoverable

When talking to Reto by voice, say the description first and never read the
exact handle aloud. Use the same rules in general agent chat and podcast drafts.
For a paper, use first-author surname, year and a topic word: “Miller 2023,
the ammonia one”. For a gripe, todo or memory, use a two-to-four-word
description such as “the endpoint split gripe”. These descriptions are derived
from the current summary and metadata; they are not stored aliases or unique IDs.

On first mention, retain the exact handle once after the description in the
transcript: `the endpoint split gripe [gr593139]`. Brackets do not guarantee
silence in every voice client. Verify once in the client; if it reads brackets,
use a Markdown link with the handle as target: `[the endpoint split gripe](gr593139)`.
Draft narration already strips inline handles; keep the description in prose.
Use a word handle only as a fallback when there is nothing useful to describe,
or when the user asks for them (below).

To recover a reference from a spoken phrase or transcript snippet, start with
`search(kind='paper', q='Miller ammonia')` (substitute the reference kind).
For a compact candidate list, use
`get(kind='handle', q='the Miller paper', args={'kind':'paper'})`.
Supported scopes are paper, gripe, todo and memory. The resolver returns up to
five `handle | spoken name | summary` lines. Compare candidates using the
conversation's topic, year and author; when ambiguous, ask which one rather than
guessing. No hits means retry search with more context. Descriptions are search
cues, so use the returned exact handle for subsequent reads and writes.

## Word handles: speak words, never read the number

A word handle is the say-out-loud form of a ref id: two common words,
adjective + noun, instead of six digits (`gr674030` is "gentle beacon").
Use them aloud when a description is unavailable or the user asks for word
handles. Say "gentle beacon" and never read the number aloud.

Off by default, so normal agent traffic pays no tokens. Turn it on and
off again for text:

```python
put(kind='handle', text='on')
put(kind='handle', text='off')
```

The ack names the scope: "for this session" on a live MCP session, or "for
this connector credential" on stateless HTTP, where the switch follows the
authenticated OAuth grant (it survives token refresh and never leaks to other
credentials). With neither a live session nor a dedicated credential (for
example the shared installation bearer) the put raises `BadInput`; reconnect
through OAuth, or convert one-off with `get(kind='handle', id=...)`. An
explicit switch beats auto-enable.

While on, search and list rows gain a `word` column, so a whole page can
be spoken without a lookup per id. The built-in Claude connector is
auto-enabled (it cannot tell voice from text), and a deployment may add more
clients by clientInfo, OAuth client id or User-Agent
(`PRECIS_WORD_HANDLE_CLIENTS`): a `word` column in your first list means the
switch is already on.

## Convert both ways and resolve a clash

```python
get(kind='handle', id=674030)
get(kind='handle', id=[674030, 674040])
get(kind='handle', q='gentle beacon')
```

- `id=` returns `674030 = gentle beacon`; a list returns one `id = words`
  line per id (a clash note rides on that id's own line).
- `q=` on two words lists the live refs sharing the pair, newest first:
  handle, three-word form, kind, size, summary. `args={'of_kind': 'memory'}`
  narrows by kind, `args={'page': N}` pages, and the header reports the true
  total. Only refs you may read are listed.
- `q=` on three words returns the exact id. The words are never accepted
  as `id=` on other kinds: resolve to the number or handle here first.

A pair encodes `id % 16384`, so two live refs can share it. When `id=`
hits a clash the answer is the three-word form plus a note naming the
others: say all three words ("cobalt gentle beacon") to pick exactly one.
When a listener gives only two words, answer from the candidate list and ask
which they meant by summary.

See also [[precis-handle-help]] (the converter) and [[precis-session-context-help]].
