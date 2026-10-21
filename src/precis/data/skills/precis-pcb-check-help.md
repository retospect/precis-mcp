---
id: precis-pcb-check-help
family: pcb
title: precis — the PCB check surface (view='check', phases, finding delta, ERC)
summary: one check surface for a pcb design — view='check' filters findings by rule/severity/phase; the board phase (netlist/placed/routed/fab) gates which rules run and tiers severities; op='phase' records the authored claim; ERC rules (pin_conflict, no_driver, over_voltage, rail_crossing, no_decoupling, over_current) join the geometric DRC on the same table; the get views are read-only and delta against the last recorded run of the same phase; put op='check' records a run
answers:
  - how do I check a pcb design — DRC, netlist checks, ERC?
  - how do I see only errors, or only one rule's findings?
  - what does the design phase mean and how do I claim fab-ready?
  - what do 'new / still / resolved' mean on a DRC run?
  - how do I declare pin electrical types and rail/voltage bounds for ERC?
applies-to: get/put (kind='pcb')
status: active
tags: [design]
kinds: [pcb]
---

# precis-pcb-check-help — one check surface

`view='check'` is THE check surface; `view='drc'` stays as its preset —
same findings, same table.

- `args={'rule':'a,b'}`/`{'severity':'error'}` filter the table;
  `{'phase':'netlist|placed|routed|fab'}` judges AT that phase:
  `netlist` runs only geometry-free rules and never reads copper;
  `placed` is pads-only; `routed`/`fab` the full pass. Only a claimed
  phase (`args.phase` or `op='phase'`) scopes the run — a derived
  `placed` board that carries copper still gets it checked.
  `connectivity` judges nets that have copper; a net with none is
  `unrouted` only.
- Severity is a function of `(rule, phase)`: `unrouted` reads `todo`
  before `routed`, `error` after; a netless pin warns below `fab`,
  errors at it (`todo` is a display tier; stored rows keep `error|warn`).
  At `netlist` the per-net `unrouted` rows collapse to one head line —
  expected before routing — `args={'rule':'unrouted'}` expands them.
- The phase derives from board state; `fab` can only be claimed:
  `put(args={'op':'phase','phase':'fab'})`, `phase: null` clears it.
- ERC joins DRC here: `pin_conflict`, `no_driver`, `over_voltage`,
  `rail_crossing` (`roles:['level_shifter']` translates), `no_decoupling`,
  `over_current`, `passive_rating` — fed by `pins[].etype`, `rail`,
  `max_voltage_v`, `max_current_a`/`current_role`, `min_dielectric`,
  `nets[].rail`/`voltage`/`current`, and the catalog `part_description`
  behind a placed C-number (annotate on an existing part via `op='pins'`
  — [[precis-pcb-help]]). A rule missing its vocabulary reports
  `not_checked`, not a pass; a pin bound on a net with no declared
  voltage/current reports `voltage|current unknown for net X: ask the
  user` — never guessed.
- `passive_rating` parses a catalog part's description (`C1525` →
  `100nF 16V X7R`) and checks it against the `fixed-derating` margin
  policy: cap rated ≥ 2× the voltage across it (≥ 1.5× for C0G/NP0),
  resistor dissipation ≤ 50 % of rated power (V²/R across its nets,
  or I²R through a `series` pin on a net with `current`), and dielectric
  rank ≥ a pin's declared `min_dielectric` (C0G > X7R > X5R > Y5V/Z5U)
  on the pin's rail. The findings name both numbers and the policy; a
  description that does not parse is a not-checked info row.
- Three rules fire only on `profile='prototype'` boards (`prod` DRC
  output is unchanged — [[precis-pcb-help]]): `via_density` (warn — more
  vias in some 10×10mm window than the profile's per-mm² ceiling allows),
  `via_under_part` (info, with a count — vias sitting under a part's
  courtyard; a soft router-cost signal, never a keep-out), `fine_pitch_pad`
  (warn — a part's own pads closer than 0.5mm centre-to-centre, hard to
  hand-solder).

## Read a run as a delta — finding ids

Every finding carries a stable `id` (12 hex chars): a content hash over
`(rule, participants, layer)` — the two nets of a clearance hit, the pad
and the via of a keep-out — never over coordinates or list position, so
the same pair at a different margin after a nudge is the SAME finding.
`view='check'`, `view='drc'` and `view='gerber'` are read-only: they
compute the run and write nothing (Reto 2026-10-16). To record one — the
durable run `netlist_drc_clean`, review and checklists read — call
`put(kind='pcb', id='<slug>', args={'op':'check'})` (takes the same
`phase`/`severity`/`rule` args; `op='review'` records one too). A read's
head says `unrecorded run`; a recorded run (clean runs too) shows its id.
Either way the head reports the delta against the last recorded run
**of the same phase** on that board: `vs run b13770b5: 3 new, 127 still (2 worse), 5 gone` — new and
gone counted separately, never netted, because a move that fixes three
and creates three is not "no change". The table's `delta` column reads
`new` / `still` / `still, worse (was -0.090)` / `still, better (was …)`;
a `## gone since run …` list names what went, by id. The first run at a
phase says "none to compare against" rather than calling everything new.

Each recorded run stores the board revision it checked (a hash over the
design, copper and cached footprints). When the board has changed since,
the head adds `stale baseline: that run was recorded <time> at revision
<a>; the board is now at revision <b>`. Every reader of the latest run
(the `netlist_drc_clean` gate, the checklist's drc-clean item, argue, the
quest board) says `stale, re-check` instead of a verdict, and a stale run
never counts as clean. Re-record with `args={'op':'check'}` to clear it.

A recorded run's findings persist in `pcb_drc_findings` with `finding_key`, `margin_mm`
and `first_seen_at` (the lifetime start — surviving a clean run in
between, so "broken again" reads `new` while keeping its original
first-seen).

Rows list errors before warnings before todos. With 10 or more findings
the head adds a `by rule:` tally (rule, count, severity). Narrow the
table with `args={'severity': 'error'|'warn'|'todo', 'rule': 'via_pad_keepout'}`
(`rule` takes a comma list; an unknown rule or severity is refused with
the valid options). The head counts stay board-wide and a
`filtered: showing N of M` line says the table is cut.

No via may sit in a pad: a via whose annulus overlaps a solder pad's copper
is an error (`via_pad_keepout`, or `via_in_pad` for an imported or authored
same-net fixed via), because a via in a pad needs a filled and capped via,
a fab upgrade we do not buy. Fix an imported one at the source board.

The fab-export path (`view='gerber'`) always judges at `fab`: an
authored `phase='placed'` claim is a progress statement, not a licence
to ship — a routed board's clearance/connectivity errors still lead the
bundle's DRC banner.
