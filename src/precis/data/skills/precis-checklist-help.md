---
id: precis-checklist-help
family: work
title: precis — checklists: argued, invalidating check ledgers
summary: checklist kind — run a checklist on a target, record argued verdicts that go stale on change, author a local checklist, gate a todo on it
answers:
  - how do I run the pre-tapeout checklist on a pcb design?
  - how do I record a checklist verdict and why does it say stale?
  - how do I waive a checklist item or argue that it does not apply?
  - how do I add my own checklist items for one board?
  - how do I make a todo wait until a checklist is clean?
applies-to: get/put/edit/search (kind='checklist'); put (kind='todo' with auto_check checklist_clean)
status: active
tags: [workflow, design]
kinds: [checklist, pcb, todo]
---

# precis-checklist-help — argued, invalidating check ledgers

A checklist is a named set of **items**, each stating the failure it
prevents. A target (a `pcb` design today; cad, se, deploy later) is
**assigned** checklists, and per-target **verdicts** accumulate in an
append-only ledger instead of restarting on every review. Two kinds of
item, deliberately distinct:

- **tool** items bridge to the target kind's own encoded rules (DRC,
  route status, netlist exceptions). Their status is read **live** off
  the checker's record; you never record a pass for them, only a
  `waived` verdict with a rationale. One coarse item per checker — the
  checklist never restates individual rules.
- **judgment** items are evaluation tasks for you: read the item, look
  at the design, decide, and record the verdict with your reasoning as
  evidence. This is where the respins DRC cannot catch live —
  representation mismatches (symbol vs footprint vs the real mating
  cable, layout vs enclosure, BOM vs what is purchasable).

Three-valued honesty is the rule: an item with no verdict reads `not
checked`, a checker that cannot fire reads `not run (<reason>)`, and
neither is ever rendered as clean.

## Which checklists apply to my target

```python
get(kind='checklist', target='pcb:sensor-node')
```

Lists explicit assignments and **kind defaults** — `pcb-tapeout` applies
to every pcb without an assign step. The bare `get(kind='checklist')`
lists every checklist; `get(kind='checklist', id='pcb-tapeout')` renders
the definition (phase, severity, decidability, `prevents` per item).

## Run a checklist on a target
## Read the status view

```python
get(kind='checklist', id='pcb-tapeout', target='pcb:sensor-node')
```

The first line is the gate verdict: `BLOCKED — n blocking failure(s)`,
`INCOMPLETE — n blocking item(s) unsettled`, or `blocking items settled`,
followed by a tally. Then one row per item, in the checklist's phase
order (pcb: schematic → netlist → layout → fab):

| status | meaning | what you do |
|---|---|---|
| `not checked` | no verdict yet | work the item, record a verdict |
| `pass` / `fail` / `n/a` / `waived` | your current verdict (judgment) or the live checker (tool) | nothing, or fix the design on `fail` |
| `stale (item revised)` | the item's text changed since you judged it | re-read, re-judge |
| `stale (target changed)` | the design changed within the verdict's scope | re-check only this item |
| `stale (target changed: U7 gone)` | an anchor you named no longer exists | re-check; anchor to what replaced it |
| `not run (…)` | a tool checker has nothing to read yet | run the checker named in the reason (e.g. `view='drc'`) |

Below the table, the **argument** section lists the item threads: open
questions first (`[OPEN]`), and `about` anchors that do not resolve on
the target flagged `[dangling: …]`.

Work phase by phase. A re-run is a diff against the ledger: current
verdicts carry forward, only `not checked` and `stale` rows need you.

## Record a verdict
## Anchor a verdict so it only goes stale when its scope changes

```python
edit(kind='checklist', id='pcb-tapeout', target='pcb:sensor-node',
     op='verdict', item='connector-mates-counterpart', verdict='pass',
     anchors=['J1', 'J2'],
     evidence={'reasoning': 'J1 is JST-PH 4-pin female; mates the PH cable in the BOM, pin 1 = GND matches the harness drawing',
               'datasheet': 'da123456'},
     checked_by='agent')
```

- `verdict` is one of `pass | fail | n/a | waived`. `n/a` is a real
  verdict ("this board has no antenna"), not a skip — items carry an
  `applies` hint for exactly this.
- `anchors` names the refdes / net names the check covered. On a pcb the
  fingerprint is computed over those sub-objects, so an unrelated edit
  elsewhere leaves the verdict current. **Omit anchors and the verdict
  covers the whole board** — it goes stale on any edit. Anchor whenever
  the item is about specific parts.
- `evidence` is a free dict: your reasoning, a DRC `run_id`, a datasheet
  handle, a measured number. For judgment items, write the argument that
  would convince a reviewer — the ledger is the review record.
- A second verdict on the same item appends; the old row stays
  queryable. Nothing is ever overwritten.

Kinds without a fingerprint bridge accept an opaque `fingerprint='…'`
string instead of `anchors`; pass the same string to `get(...,
fingerprint=...)` to detect change yourself.

## Waive an item
## Argue that an item does not apply here

A waiver is a verdict with a mandatory rationale:

```python
edit(kind='checklist', id='pcb-tapeout', target='pcb:sensor-node',
     op='verdict', item='drc-clean', verdict='waived',
     evidence={'rationale': 'the one clearance error is the castellated edge pad JLC accepts at 0.15 mm; confirmed against their capability page'})
```

A current waiver is the only ledger verdict that overrides a live tool
`fail`. Prefer `n/a` when the item genuinely does not apply; use
`waived` when it applies and you are deliberately not meeting it.

## Ask and answer in the item's argument thread

```python
edit(kind='checklist', id='pcb-tapeout', target='pcb:sensor-node',
     op='add_note', item='decoupling-adequate', name='q-u3-vdda',
     note_kind='question', body='U3 VDDA has one 100n — is the 1u the datasheet wants on the LDO side enough?',
     about=['U3', 'VDDA'])
edit(kind='checklist', id='pcb-tapeout', target='pcb:sensor-node',
     op='add_note', item='decoupling-adequate', name='d-u3-vdda',
     note_kind='decision', re='q-u3-vdda',
     body='no — add 1u at U3 pin 9; the LDO cap is 30 mm away through a via')
```

`note_kind` is `question | answer | decision`; `re` names the note being
answered, which settles the question (open/settled is derived, never
stored). `about` anchors the note to refdes/net names or another item
name; a dangling anchor is reported at read time, not rejected.
`op='remove_note'` retires a note (a true retraction — corrections are
new notes).

## Author a local checklist
## Add an item for one board only

Shipped checklists (`pcb-tapeout`) are owned by the repository: `edit`
on a shipped item is refused. Your two outlets:

```python
# a board-specific concern, live immediately, scoped to one target
edit(kind='checklist', id='pcb-tapeout', op='add_item',
     item='sensor-node-battery-polarity-key',
     target='pcb:sensor-node', phase='layout', severity='blocking',
     prevents='the unkeyed battery lead plugs in reversed and the LDO has no reverse protection')

# a whole new checklist (every item needs prevents — no failure statement, no item)
put(kind='checklist', id='bench-fixture-preflight', default_for=['pcb'],
    items=[{'name': 'probe-clearance', 'phase': 'layout', 'severity': 'advisory',
            'prevents': 'a test point is buried under a tall part and cannot be probed'}])
edit(kind='checklist', id='bench-fixture-preflight', op='assign', target='pcb:sensor-node')
```

A generally useful item: add it locally **and** file a `gripe` asking
for it in the shipped file. When the shipped version lands, the next
deploy's sync supersedes your local rev by name — nothing waits on a
deploy to be usable. `op='retire_item'` retires a local item;
`op='unassign'` removes an explicit assignment (kind defaults cannot be
unassigned — record `n/a` verdicts instead).

## Gate a todo on a checklist

```python
put(kind='todo', text='[auto] sensor-node tapeout gate', parent_id=98,
    meta={'auto_check': {'type': 'checklist_clean',
                         'checklist': 'pcb-tapeout', 'target': 'pcb:sensor-node'}})
```

The leaf resolves when every **blocking** item is settled with no
`fail`; it stays parked while anything blocking is `not checked`,
`stale`, or `not run`. Advisory items never block. Details of the
auto_check mechanism: [[precis-auto-todo-help]].

## The pcb-tapeout checklist in one pass

1. `schematic` — judgment items on the netlist's intent: connector
   mates its real counterpart, rail budgets and dropout, decoupling,
   straps vs datasheet, reset, unused pins, protection, test points,
   bring-up and programming provisions.
2. `netlist` — `netlist-exceptions-clean` (tool: pins on no net, nets
   with one member; argue and waive the deliberate ones) and declared
   proximity intents.
3. `layout` — `drc-clean` and `all-nets-routed` (tool; run
   `get(kind='pcb', id=..., view='drc')` first) plus footprint,
   polarity silk, mechanical fit, return paths, clocks, thermals,
   residual soft measures, jig-ready test pads.
4. `fab` — stackup vs what JLC builds, BOM lifecycle (re-check before
   each order — the world moves even when the board does not), Gerber
   integrity, paste, panelization, assembly drawing, fasteners, and a
   written bring-up procedure.

Re-run the status view until the first line reads `blocking items
settled`. The pcb kind itself: [[precis-pcb-help]].
