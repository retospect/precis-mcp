---
id: precis-conv-help
title: precis — past conversations, durable and searchable
status: shipped
tier: 1
floor: any
applies-to: get / search / tag / link (kind='conv')
last-updated: 2026-05-02
---

# precis-conv-help — durable conversation transcripts

`conv` is where past conversations live. One conversation per slug,
one block per message turn. Populated by the **chat-bridge on
capture** — the agent surface is read-only. You can't `put()` a
transcript (that's the bridge's job), but you can search them, tag
them, and link them to anything.

## Address shape

Slug = date + short handle:

```
2026-04-26-spec          # 2026-04-26, "spec" as topic anchor
2026-04-28-tag-axes
2026-05-01-register-debug
```

Block selector addresses one message turn:

```python
get(kind='conv', id='2026-04-26-spec')              # overview + TOC
get(kind='conv', id='2026-04-26-spec~14')           # turn 14
get(kind='conv', id='2026-04-26-spec~14..20')       # range
get(kind='conv', id='2026-04-26-spec/toc')          # message-level TOC
```

## Browse

```python
get(kind='conv')                      # recent transcripts
get(kind='conv', id='/recent')        # same, explicit
```

## Search

Block-level hybrid search — lexical + semantic, same path as every
other searchable kind:

```python
search(kind='conv', q='why we dropped mode=')
search(kind='conv', q='tag axis', scope='2026-04-28-tag-axes')
```

Cross-kind works too (`kind='*'` or `kind='conv,memory'`) — a
question about "why did we decide X" will often land hits in both
`conv` and `memory`.

## Typical uses

- **Quote past decisions.** "We already debated this on 2026-04-26
  — let me find the block."
- **Recover context.** Agent returning to a long-running project
  pulls the last few conv transcripts to re-ground.
- **Cite in memory / quest.** When you write a decision note, link
  the conv block where it was debated:

  ```python
  put(kind='memory', text='Dropped mode=; typed kwargs instead.',
      tags=['kind:decision'],
      link='conv:2026-04-26-spec~14', rel='derived-from')
  ```

- **Detect repeated patterns.** Searching `conv` for a pattern
  ("why is error message X confusing") surfaces every prior
  conversation where the same friction came up. Candidate gripe.

## What you cannot do

- **Put / edit / delete from the agent surface.** Transcripts are
  immutable artefacts of what happened. Fix the future, not the
  past. If a conversation needs annotation, create a `memory` and
  link it.
- **Reorder turns.** Block pos is the capture order; it's not
  editable.
- **Bulk export.** The MCP surface reads one at a time. For bulk
  operations (export, backup), talk to the store directly.

## Tag and link

Open tags and closed `CACHE:` work; no `STATUS:` / `PRIO:` (conv is
capture, not workflow). Cross-links to any kind are free:

```python
tag(kind='conv', id='2026-04-26-spec', add=['project-precis-v2', 'pivotal'])
link(kind='conv', id='2026-04-26-spec~14',
     target='memory:88', rel='derived-into')
```

## See also

- `precis-memory-help` — annotate conversations with decisions / lessons
- `precis-relations` — `derived-from` / `supports` / `cites` vocabulary
- `precis-overview` — verbs and kinds
