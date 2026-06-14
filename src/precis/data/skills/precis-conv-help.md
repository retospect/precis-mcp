---
id: precis-conv-help
title: precis — find, read, tag past conversations
summary: captured chat transcripts — address by slug, read turns, browse recent conversations
applies-to: get/search/put/tag/link (kind='conv')
status: active
---

# precis-conv-help — find, read, tag past conversations

Conversations are captured chat transcripts. One ref per conversation,
one block per message turn. Address by date-stamped slug
(`2026-04-26-spec`); the `conv:` prefix and numeric ref ids
(`conv:73`) are also accepted as link targets.

## Read a past conversation
## Open a conversation by slug
## I have a date-stamped slug — show me the conversation

```python
get(kind='conv', id='2026-04-26-spec')                   # overview + turn count
get(kind='conv', id='2026-04-26-spec/transcript')        # full transcript
get(kind='conv', id='2026-04-26-spec~14')                # single turn
get(kind='conv', id='conv:2026-04-26-spec')              # prefix form (link-target shape)
```

Turn selector is `~N` (single turn). Path view is `/transcript`. No
range selector — for a span, fetch `/transcript` and scan.

## Browse recent conversations
## List captured transcripts
## What conversations are in the store?

```python
get(kind='conv')                  # 20 most recent
get(kind='conv', id='/recent')    # explicit
```

## Find a past decision or discussion
## Search across all conversations
## What did we decide about X?

```python
search(kind='conv', q='why we dropped mode=')
search(kind='conv', q='tag axis', scope='2026-04-28-tag-axes')   # scope to one conv
search(kind='conv', q='register endpoint', page_size=20)
```

Lexical search over turn text. Results are `slug~pos` handles; paste
one as `id=` to read the turn. Cross-kind search
(`search(kind='*', q='...')`) folds conv hits in with paper / memory
matches when a question spans both.

## Link a decision back to the conversation that produced it
## Cite a turn from memory or a citation
## How do I reference a past discussion?

```python
put(kind='memory',
    text='Dropped mode= in favour of typed kwargs.',
    tags=['topic:api-design'],
    link='conv:2026-04-26-spec~14',
    rel='derived-from')

link(kind='conv', id='2026-04-26-spec~14',
     target='memory:88', rel='derived-into')
```

Use a turn handle (`slug~pos`) as the link target to point at a
specific message; use the bare slug to point at the conversation.

## Tag a conversation
## Mark a conversation as pivotal or topic:X
## Categorise a transcript

```python
tag(kind='conv', id='2026-04-26-spec', add=['topic:api-design', 'pivotal'])
tag(kind='conv', id='2026-04-26-spec', remove=['pivotal'])
```

Open tags only. Closed workflow axes (`STATUS:`, `PRIO:`) are
rejected — `conv` is capture, not workflow. Tag and link operate at
the conversation level; turn selectors are rejected.

## Capture a new conversation

Transcripts arrive via the chat-bridge. The Hermes Discord adapter
calls `put(kind='conv', ...)` once per inbound user message and
once per outbound assistant reply:

```python
put(kind='conv',
    id='discord/<guild>/<channel>/<thread>',     # slug — stable per thread
    text='hi, what do we know about X?',         # message body
    author='alice#1234',                         # who said it
    msg_id='1185923456789012345',                # platform id — idempotency key
    title='X discussion',                        # optional, set on first call only
    ref_meta={'platform': 'discord',
              'guild_id': '...', 'channel_id': '...',
              'thread_id': '...'},               # set on first call only
    meta={'ts': '2026-06-11T10:00:00Z'})         # per-turn extras
```

The first call mints the conv ref; later calls append a turn
(block) and skip silently if `msg_id` is already captured. So a
bridge replay after a disconnect is safe — no duplicates.

To annotate a conversation in flight from an agent, create a
`memory` and `link=` it to the turn; do not call
`put(kind='conv')` from agent code (the bridge will already be
capturing the same turn).

## See also

```python
get(kind='skill', id='precis-overview')         # verbs and kinds
get(kind='skill', id='precis-search-help')      # search mechanics
get(kind='skill', id='precis-memory-help')      # annotate a conversation
get(kind='skill', id='precis-relations')        # derived-from, supports, cites
get(kind='skill', id='precis-tags')             # tag vocabulary
```
