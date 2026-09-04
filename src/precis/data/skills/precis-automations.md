---
id: precis-automations
title: precis — find and edit standing automations (recurring agent behaviours)
summary: recurring agent behaviours (the morning/evening podcast casts, the news briefing) are recurring (meta.schedule set) todos; find them with search(kind='todo', view='roots') (the Watches umbrella), edit behaviour by editing the recurring's text, link produced artifacts back with derived-into
answers:
  - how do I find the recurring todos that drive automated behaviours like the podcast cast?
  - how do I change what an automation does?
  - how do I mark a recurring todo as an automation?
  - how do I link an automation to the artifact it produced?
applies-to: recurring (meta.schedule set) todos; search(kind='todo', view='roots'); the podcast casts + briefing
status: active
---

# precis-automations — the index of things that run *you*

Some recurring (`meta.schedule` set) todos aren't ordinary scheduled work — they are
**standing automations**: recurring prompts that drive Asa to *do* something
on a schedule. The morning/evening **podcast casts** and the daily **news
briefing** are the headline examples.

Two ways a recurring tick can drive an automation:

* **Push (`meta.deliver`)** — the recurring carries
  `meta.deliver={'target': 'conv:discord/<g>/<c>/<t>'}`. A due tick fires a
  synthetic prompt at asa_bot via `pg_notify('precis.cron', ...)` built from
  the recurring's own text; asa_bot drives a full Claude turn against it and
  posts the response. No subtask lands in the doable queue — the tick's
  action *is* the delivery.
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
search(kind="todo", view="roots")  # → "## Watches (N recurring)" section
```

The de-facto discovery path: `view='roots'` lists every recurring
(`meta.schedule` set) todo under a **Watches** umbrella — the podcast
casts, the news poll, the morning briefing. The `automation`
tag described below is not applied at mint time (`search(kind='todo',
tags=['automation'])` returns nothing live) — don't rely on it as a
filter until that's fixed.

## Mark a recurring as an automation

```python
tag(kind="todo", id=42, add=["automation", "cast-morning"])
```

Un-mark with `remove=['automation']`. Marking is additive and needs no
schema change — `automation` is a normal open tag on `kind='todo'`; a
second open tag can name *which* one (`cast-morning`, `cast-evening`,
`briefing`) as a curated convention, kept short and kebab-cased. Since
nothing mints with this tag today, treat it as an opt-in label to add
by hand, not a reliable filter — use `view='roots'` to find automations.

## Edit what an automation does — edit its text (push-mode) or params (job-mode)

**Push-mode** (`meta.deliver` set): the automation's behaviour lives in the
recurring's own text — that's the synthetic prompt fired on each tick.
`edit(kind='todo', id=42, mode='replace', text='<revised prompt>')` changes it
in place (todo `edit` supports rewriting the title — no delete +
re-create dance is needed).

**Job-mode** (`meta.executor`/`meta.job_type` set): behaviour is mostly code
(the job_type's implementation) plus its `meta.params` — those aren't
editable in place today; changing them means re-creating the recurring.

## Link an automation to what it produces

After Asa publishes an artifact on a fire, link it back so the recurring
becomes a navigable hub — from the recurring you can reach its episodes, and
from an episode its editable prompt:

```python
link(
    kind="todo", id=42, target="draft:cast-reading-2026-07-16", rel="derived-into"
)  # inverse derived-from surfaces from the draft
```

Put this instruction *in the recurring's payload* (push-mode) so it happens
on every fire.

## See also

```python
get(
    kind="skill", id="precis-recurring-help"
)  # the unified schedule mechanism (cron/every/at, deliver)
get(kind="skill", id="precis-voice")  # how to author a cast payload for the ear
get(kind="skill", id="precis-audio-help")  # the narration + podcast-feed mechanism
```
