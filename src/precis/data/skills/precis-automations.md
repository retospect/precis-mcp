---
id: precis-automations
title: precis — find and edit standing automations (recurring agent behaviours)
summary: recurring agent behaviours (the morning/evening podcast casts, the news briefing) are cron refs tagged `automation`; find them with get(kind='cron', id='/automations'), edit behaviour by editing the cron payload, link produced artifacts back with derived-into
applies-to: cron refs tagged 'automation'; get(kind='cron', id='/automations'); the podcast casts + briefing
status: active
---

# precis-automations — the index of things that run *you*

Some `cron` refs aren't one-shot reminders — they are **standing automations**:
recurring prompts that drive Asa to *do* something on a schedule. The
morning/evening **podcast casts** and the daily **news briefing** are the
headline examples. A cron fires `pg_notify('precis.cron')` at its
`next_fire_at`; asa_bot turns the cron's payload into a synthetic user turn and
drives Asa against it, and Asa produces the artifact (a narrated `draft` → an
episode on the podcast feed). There is **no separate producer** — the cron's
payload *is* the prompt that shapes the output.

## Find the automations

```python
get(kind='cron', id='/automations')          # standing automations, by next fire
search(kind='cron', tags=['automation'])      # same set, searchable
search(kind='cron', tags=['automation', 'cast-morning'])   # one automation
```

A cron is an automation when it carries the **`automation`** tag. A second open
tag names *which* one (`cast-morning`, `cast-evening`, `briefing`). This is a
curated convention, not a validated axis — keep the subtype short and
kebab-cased.

## Mark a cron as an automation

```python
tag(kind='cron', id=42, add=['automation', 'cast-morning'])
```

Un-mark with `remove=['automation']`. Marking is additive and needs no schema
change — `automation` is a normal tag on the `cron` kind.

## Edit what an automation does — edit its payload

The automation's **behaviour lives in the cron payload** (the `cron_payload`
chunk). To change tone, length, or content of a cast, edit that payload. Cron
`put` rejects `id=` (create-only), so to change a payload you **re-create**:

```python
get(kind='cron', id=42)                       # read the current schedule + payload
tag(kind='cron', id=42, add=['STATUS:paused'])   # pause the old one
put(kind='cron', text='<the new, revised payload>',
    recurring='daily@06:00', target='conv:discord/<g>/<c>/<t>',
    tags=['automation', 'cast-morning'])      # re-create with the new prompt
delete(kind='cron', id=42)                    # cancel the old one once happy
```

(Pausing first lets you verify the replacement before cancelling the original.)

## Link an automation to what it produces

After Asa publishes an artifact on a fire, link it back so the cron becomes a
navigable hub — from the cron you can reach its episodes, and from an episode
its editable prompt:

```python
link(kind='cron', id=42, target='draft:cast-reading-2026-07-16',
     rel='derived-into')       # inverse derived-from surfaces from the draft
```

Put this instruction *in the cron payload* so it happens on every fire.

## See also

```python
get(kind='skill', id='precis-cron-help')   # the scheduling mechanism (fire, recurrence, catch-up)
get(kind='skill', id='precis-voice')       # how to author a cast payload for the ear
get(kind='skill', id='precis-audio-help')  # the narration + podcast-feed mechanism
```
