---
id: precis-message-help
title: precis — proactive outbound messages (Discord posts)
summary: proactive outbound messaging — unprompted channel posts, stored for introspection
applies-to: get/search/put/delete/tag/link (kind='message')
status: active
---

# precis-message-help — ping the user without being prompted

`message` is the verb you use to send Discord messages *without*
waiting for the user to message you first. Every send is stored as
a searchable ref — useful for "what have I been nagging the user
about this week?" introspection.

Numeric ids. Body lives as a `message_body` chunk.

## Post a message to a Discord channel/thread

```python
put(kind='message',
    text='Hey, you said you would ship that PR today — still on track?',
    target='discord/<guild>/<channel>/<thread>')
```

The target points at the same conversation slug shape used by
`conv` refs. Asa_bot LISTENs on the `precis.messages` pg_notify
channel and delivers immediately.

## Post with attachments

```python
put(kind='message',
    text='here is the lit-review I drafted',
    target='discord/<guild>/<channel>/<thread>',
    attachments=[
        {'filename': 'lit-review.md',
         'content_type': 'text/markdown',
         'archive_path': '/opt/nfs/.../reports/lit-review.md'},
        {'filename': 'diagram.png',
         'content_type': 'image/png',
         'archive_path': '/opt/nfs/.../reports/diagram.png'},
    ])
```

Attachments must already exist at `archive_path` (on NFS, visible
to asa_bot). The delivery layer uploads them inline.

## Annotate why you're pinging (audit trail)

```python
put(kind='message',
    text='cluster postmortem deadline tomorrow',
    target='discord/<g>/<c>/<t>',
    reason='cron:42 fired',
    link='memory:217', rel='derived-from')
```

`reason=` is a free-form short trace string. The link to the
motivating context (a memory, a cron, a conv turn) makes future
"why did I send this?" debugging instant.

## Inspect, cancel, search

```python
get(kind='message', id=42)              # full message + status + reason
get(kind='message', id='/recent')       # recent sends across all targets
search(kind='message', q='PR status')   # find past sends by content

delete(kind='message', id=42)           # cancel before delivery
```

A message that hasn't yet been delivered (`meta.status='queued'`)
can be soft-deleted. asa_bot won't deliver it. After delivery
(`meta.status='sent'`), delete just removes it from the agent
surface; the actual Discord message stays posted.

## What happens after `put`

1. Handler stores the ref + body chunk in postgres.
2. Same transaction: emits `pg_notify('precis.messages',
   '{"ref_id": N, "target": "..."}')`.
3. asa_bot LISTENs on that channel, picks up the notification.
4. asa_bot fetches the ref + chunks + attachments, posts to Discord
   via the appropriate transport.
5. asa_bot stamps `meta.status='sent'` (or `'failed'` with an
   error trace).

You don't see asa_bot. You just see the message land.

## Rate sense

Don't spam. The cost of a wrongly-timed proactive ping is real —
the user lives with their phone in their pocket. Use messages for:

- Cron fires (you scheduled this; the user expected it)
- Verified results (researcher came back with the paper you queued)
- Real-time anomalies (a watch detected drift)

NOT for:

- "Still working on it" status updates (use a Discord typing
  indicator if anything — asa_bot handles that automatically)
- Filler / acknowledgement
- Anything you'd be annoyed to receive on your own phone

When in doubt, don't send. The user can always pull state via a
fresh question; you can't unsend a ping.

## See also

```python
get(kind='skill', id='precis-cron-help')        # schedule future sends
get(kind='skill', id='precis-conv-help')        # captured conversations
get(kind='skill', id='precis-overview')         # verbs and kinds
```
