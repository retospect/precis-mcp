---
id: precis-pcb-review-help
family: pcb
title: precis — the pcb review pass (op='review', the pcb_review job, pcb-tapeout verdicts)
summary: put(kind='pcb', args={'op':'review'}) queues a pcb_review job that runs the check surface fresh, then works the not-checked and stale pcb-tapeout judgment items, one fresh-context model call each; verdicts are machine-origin, anchored to refdes/nets, quote the decisive line and record the part-fact and margin revisions they read; missing datasheet facts read 'not checkable', never pass; a re-run on an unchanged design makes no model call
answers:
  - how do I review a pcb design against the tapeout checklist?
  - what does the Review button on the quest board do?
  - why does an item say not checkable, and how do I make it checkable?
  - when does a review verdict go stale?
  - how do I re-run the review for one item?
applies-to: put (kind='pcb'), put (kind='job')
status: active
tags: [design]
kinds: [pcb, checklist, job]
---

# precis-pcb-review-help — the review pass

`put(kind='pcb', id=<slug>, args={'op':'review'})` queues one
`pcb_review` job over the `pcb-tapeout` **judgment** items whose status is
`not checked` or `stale (…)`. When nothing is queued it says so and mints
no job (and no check run is written).

The job itself starts by running the check surface fresh — the same
persisted run `view='check'` makes (geometric DRC + ERC, at the design's
phase) — and its summary names it (`check run <id>: N finding(s)`). The put
runs no check.

`args.item='<name>'` reviews one item; `args.page_size=N` (1–60, default
15; `limit` the deprecated alias)
caps the items one job works, and a re-run continues with the rest. The
quest board's **Review** button sends the same `put`.

## The walk (what each item's reviewer sees)

Each item gets one fresh-context call: the reviewer did not design the
board or extract its facts. Its evidence block:

- the check run it just recorded (by id) — fix `netlist` ERC findings
  first (`view='check', args={'phase':'netlist'}` shows them), since the
  review reads them;
- every part with its datasheet facts (`get(kind='part', id='C…',
  view='facts')`): per pin domain, levels and current, required
  externals, strap/address tables — or `no datasheet facts for C…`;
- the nets with their members.

Per IC the reviewer works: part facts → per pin domain, levels, current,
strap/address → required externals present and adequate → a verdict
anchored to the refdes/nets it is about.

## What gets written

- **pass / fail / n/a** — a `machine`-origin verdict, `checked_by=
  'pcb_review'`, anchors narrowed to the named refdes/nets, `evidence`
  `{reason, quote, job}`. `inputs` records `part:<C#>` (datasheet-facts
  rev) for each anchored part — every part for a whole-board verdict —
  and `margin:fixed-derating` when the reviewer used the margin policy.
- **not checkable** — a `machine`-origin verdict `not checkable` with
  `evidence` `{reason: <what is missing>, job}`, the same anchors and
  `inputs` as a pass/fail. It is never a pass: the status line reads
  `not checkable`, evidence `missing: <reason>`, and a blocking item
  holding one keeps tape-out open (INCOMPLETE). It is a current record,
  so the item is not re-asked until the verdict goes stale like any
  other (item revised, anchors changed, or an input moves — e.g. a
  `datasheet_facts` job lands for the missing part), then reviewed again.

Read the result: `get(kind='checklist', id='pcb-tapeout',
target='pcb:<slug>')`.

## Staleness and re-runs

A verdict goes `stale` when its anchors change (`target changed`), the
item is revised, or a recorded input moves (`input changed: part:C…` /
`margin:fixed-derating`). The next review re-works exactly those.
Settled items are never re-asked, so a re-run on an unchanged design
costs no model call.

Human verdicts (`edit(kind='checklist', op='verdict', origin='human')`)
are kept: a stale human verdict is shown stale and re-reviewed, never
dropped. `edit(kind='checklist', op='forget', target=…)` retires
machine verdicts only, so the next pass recomputes them.

## Margin policy (`fixed-derating`, revision 1)

Capacitor rated voltage ≥ 2 × the working voltage for X5R/X7R, ≥ 1.5 ×
for C0G/NP0; resistor dissipation ≤ 50 % of rated power. Dielectric rank
C0G > X7R > X5R > Y5V/Z5U against the part fact's minimum. The
deterministic `passive_rating` rule (precis-pcb-check-help) applies the
same revision; a bump marks every verdict that recorded it stale.

## Not here

The review proposes, it does not edit the board — argue changes with
`pcb_argue` (precis-pcb-help). Part-fact extraction is
precis-datasheet-help.
