---
id: precis-automations
title: precis — find and edit standing automations (recurring agent behaviours)
summary: recurring agent behaviours (the morning/evening podcast casts, the news briefing) are level:recurring todos tagged `automation`; find them with search(kind='todo', tags=['level:recurring', 'automation']), edit behaviour by editing the recurring's text, link produced artifacts back with derived-into
applies-to: level:recurring todos tagged 'automation'; search(kind='todo', tags=['level:recurring', 'automation']); the podcast casts + briefing
status: active
---

# precis-automations — the index of things that run *you*

Some `level:recurring` todos aren't ordinary scheduled work — they are
**standing automations**: recurring prompts that drive Asa to *do* something
on a schedule. The morning/evening **podcast casts** and the daily **news
briefing** are the headline examples.

Two ways a recurring tick can drive an automation (ADR 0061 folded the
formerly-separate `kind='cron'` push mechanism onto `level:recurring`):

* **Push (`meta.deliver`)** — the recurring carries
  `meta.deliver={'target': 'conv:discord/<g>/<c>/<t>'}`. A due tick fires a
  synthetic prompt at asa_bot (`pg_notify('precis.cron', ...)`, same wire
  shape the retired `cron` kind used) built from the recurring's own text;
  asa_bot drives a full Claude turn against it and posts the response. No
  subtask lands in the doable queue — the tick's action *is* the delivery.
* **Deterministic job (`meta.executor` + `meta.job_type`)** — the recurring
  carries `meta.executor='claude_inproc'` + `meta.job_type='briefing'` (etc);
  a due tick mints a subtask child the `claude_inproc` dispatcher runs
  in-process (no LLM subprocess for a deterministic pass). See
  `precis-recurring-help` for this half of the mechanism; it predates and is
  unaffected by ADR 0061.

Either way, there is **no separate producer process** — the recurring's own
text/params *are* the prompt that shapes the output.

## Find the automations

```python
search(kind='todo', tags=['level:recurring', 'automation'])
search(kind='todo', tags=['level:recurring', 'automation', 'cast-morning'])
```

A recurring is an automation when it carries the **`automation`** tag. A
second open tag names *which* one (`cast-morning`, `cast-evening`,
`briefing`). This is a curated convention, not a validated axis — keep the
subtype short and kebab-cased.

## Mark a recurring as an automation

```python
tag(kind='todo', id=42, add=['automation', 'cast-morning'])
```

Un-mark with `remove=['automation']`. Marking is additive and needs no
schema change — `automation` is a normal open tag on `kind='todo'`.

## Edit what an automation does — edit its text (push-mode) or params (job-mode)

**Push-mode** (`meta.deliver` set): the automation's behaviour lives in the
recurring's own text — that's the synthetic prompt fired on each tick.
`edit(kind='todo', id=N, mode='replace', text='<revised prompt>')` changes it
in place (todo `edit` supports rewriting the task line; unlike the retired
cron kind's create-only `put`, no delete + re-create dance is needed).

**Job-mode** (`meta.executor`/`meta.job_type` set): behaviour is mostly code
(the job_type's implementation) plus its `meta.params` — those aren't
editable in place today; changing them means re-creating the recurring.

## Link an automation to what it produces

After Asa publishes an artifact on a fire, link it back so the recurring
becomes a navigable hub — from the recurring you can reach its episodes, and
from an episode its editable prompt:

```python
link(kind='todo', id=42, target='draft:cast-reading-2026-07-16',
     rel='derived-into')       # inverse derived-from surfaces from the draft
```

Put this instruction *in the recurring's payload* (push-mode) so it happens
on every fire.

## See also

```python
get(kind='skill', id='precis-recurring-help')  # the unified schedule mechanism (cron/every/at, deliver)
get(kind='skill', id='precis-voice')           # how to author a cast payload for the ear
get(kind='skill', id='precis-audio-help')      # the narration + podcast-feed mechanism
```
