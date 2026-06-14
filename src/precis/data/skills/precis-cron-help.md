---
id: precis-cron-help
title: precis — schedule wakeups, reminders, recurring tasks
summary: scheduling future prompts — wakeups, reminders, delivery to a target conversation
applies-to: get/search/put/delete/tag/link (kind='cron')
status: active
---

# precis-cron-help — schedule a future thought

`cron` schedules a payload (a natural-language prompt) to be
delivered to a target conversation at a future time. The delivery
layer (asa_bot) wakes you with the payload as a synthetic user
message — you then respond as if the user had just asked.

Numeric ids. Body lives as a `cron_payload` chunk so it's
searchable through the standard surface.

## Remind me in N minutes / hours / days

```python
put(kind='cron',
    text='ask about the PR status',
    in_='10 minutes',
    target='conv:discord/<guild>/<channel>/<thread>')
```

`in_` accepts `<N> <unit>` where unit is one of:
minute / minutes / min / m / hour / hours / hr / h / day / days / d.

## Remind me at an absolute time

```python
put(kind='cron',
    text='check on the merge freeze status',
    when='2026-06-12T09:00:00Z',
    target='conv:discord/<guild>/<channel>/<thread>')
```

`when=` is ISO 8601 (Z or +00:00 both work).

## Recurring schedule

```python
# Every day at 09:00 UTC
put(kind='cron', text='morning standup ping',
    recurring='daily@09:00',
    target='conv:discord/<g>/<c>/<t>')

# Weekly on Mondays at 10:00 UTC
put(kind='cron', text='weekly review of stuck quests',
    recurring='weekly@mon@10:00',
    target='conv:discord/<g>/<c>/<t>')

# Every N minutes / hours / days
put(kind='cron', text='check the api monitor',
    recurring='every 15 minutes',
    target='conv:discord/<g>/<c>/<t>')

# Shorthand for whole intervals
put(kind='cron', text='nudge me hourly',
    recurring='hourly',
    target='conv:discord/<g>/<c>/<t>')
# (also: 'daily', 'weekly')
```

Recurring grammar (v1):
- `hourly` / `daily` / `weekly` — top-of-the-interval
- `every <N> <unit>` — fires N units after the previous fire
- `daily@HH:MM` — UTC time-of-day
- `weekly@<mon|tue|wed|thu|fri|sat|sun>@HH:MM` — weekly at UTC time

## Catch-up policy when overdue

If the machine was off when a cron should have fired:

**One-shot** (no `recurring`) — default `catch_up=True`. The cron fires
when the next tick runs. Set `catch_up=False` to mean "fire at the
moment or not at all":

```python
put(kind='cron', text='wish them happy hour @ 17:00',
    when='2026-06-11T17:00:00Z',
    catch_up=False,
    target='conv:discord/<g>/<c>/<t>')
```

**Recurring** — default `catch_up=False`. A daily-9am cron missed
because you were asleep until 11am does NOT fire late; the next fire
is tomorrow at 9am. Set `catch_up=True` to fire ONE catch-up when
overdue (never multiple — even after a 3-day outage):

```python
put(kind='cron', text='daily housekeeping',
    recurring='daily@03:00', catch_up=True,
    target='conv:discord/<g>/<c>/<t>')
```

## Inspect, pause, cancel

```python
get(kind='cron', id=42)              # full schedule + status + history
get(kind='cron', id='/recent')       # recent crons (any status)
search(kind='cron', q='PR')          # find by payload text

delete(kind='cron', id=42)           # cancel (soft-delete)
tag(kind='cron', id=42, add=['STATUS:paused'])   # pause without delete
tag(kind='cron', id=42, remove=['STATUS:paused']) # resume
```

The `STATUS:` tag pauses the entry without losing it. The tick CLI
skips entries whose `meta.status != 'scheduled'`, and paused entries
read as `STATUS:paused` in the tag list.

## Link a cron to its motivating context

```python
# Recommended: link the cron back to the conversation or memory
# that motivated it, so future searches surface the relationship.
put(kind='cron',
    text='follow up on the cluster postmortem',
    in_='3 days',
    target='conv:discord/<g>/<c>/<t>',
    link='memory:217', rel='derived-from')
```

## What happens when a cron fires

1. The cron-tick CLI (launchd timer every 60s on melchior) finds the
   due entry, marks status, advances `next_fire_at`.
2. PG NOTIFY on channel `precis.cron` carries the payload + target.
3. asa_bot LISTENs, fetches the cron ref, and delivers the payload
   as a synthetic user message to the target conversation.
4. Asa responds as usual — exactly as if the user had typed the
   payload. Same MCP tools, same context-building, same capture.

You don't need to know the wakeup mechanics. Just write a good
payload (what you want to be reminded about) and a sensible time.

## See also

```python
get(kind='skill', id='precis-overview')         # verbs and kinds
get(kind='skill', id='precis-message-help')     # proactive sends
get(kind='skill', id='precis-memory-help')      # sticky memory + TTL
```
