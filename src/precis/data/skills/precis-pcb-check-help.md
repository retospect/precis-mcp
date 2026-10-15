---
id: precis-pcb-check-help
family: pcb
title: precis — the PCB check surface (view='check', phases, finding delta, ERC)
summary: one check surface for a pcb design — view='check' filters findings by rule/severity/phase; the board phase (netlist/placed/routed/fab) gates which rules run and tiers severities; op='phase' records the authored claim; ERC rules (pin_conflict, no_driver, over_voltage, rail_crossing, no_decoupling, over_current) join the geometric DRC on the same table; every run persists and deltas against the previous run of the same phase
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

## Read a run as a delta — finding ids

Every finding carries a stable `id` (12 hex chars): a content hash over
`(rule, participants, layer)` — the two nets of a clearance hit, the pad
and the via of a keep-out — never over coordinates or list position, so
the same pair at a different margin after a nudge is the SAME finding.
Each check/drc call is a recorded run (clean runs too) and the head
reports the delta against the previous run **of the same phase** on that
board: `vs run b13770b5: 3 new, 127 still (2 worse), 5 gone` — new and
gone counted separately, never netted, because a move that fixes three
and creates three is not "no change". The table's `delta` column reads
`new` / `still` / `still, worse (was -0.090)` / `still, better (was …)`;
a `## gone since run …` list names what went, by id. The first run at a
phase says "none to compare against" rather than calling everything new.
Findings persist in `pcb_drc_findings` with `finding_key`, `margin_mm`
and `first_seen_at` (the lifetime start — surviving a clean run in
between, so "broken again" reads `new` while keeping its original
first-seen).

Rows list errors before warnings before todos. With 10 or more findings
the head adds a `by rule:` tally (rule, count, severity). Narrow the
table with `args={'severity': 'error'|'warn'|'todo', 'rule': 'via_pad_keepout'}`
(`rule` takes a comma list; an unknown rule or severity is refused with
the valid options). The head counts stay board-wide and a
`filtered: showing N of M` line says the table is cut.

The fab-export path (`view='gerber'`) always judges at `fab`: an
authored `phase='placed'` claim is a progress statement, not a licence
to ship — a routed board's clearance/connectivity errors still lead the
bundle's DRC banner.
