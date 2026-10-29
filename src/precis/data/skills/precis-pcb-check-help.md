---
id: precis-pcb-check-help
family: pcb
title: precis — the PCB check surface (view='check', phases, finding delta, ERC, footprint and pin fixes)
summary: one check surface for a pcb design — view='check' filters findings by rule/severity/phase; the board phase (netlist/placed/routed/fab) gates which rules run and tiers severities; op='phase' records the authored claim; ERC rules (pin_conflict, no_driver, over_voltage, rail_crossing, no_decoupling, over_current) join the geometric DRC on the same table; the get views are read-only and delta against the last recorded run of the same phase; put op='check' records a run; fixes: op='footprint' for synthesized_footprint, pads vs pins, op='pin_rename'/'pin_remove', op='pins' for ERC pin facts, op='pad' for a per-instance pad move/resize
answers:
  - how do I check a pcb design — DRC, netlist checks, ERC?
  - how do I see only errors, or only one rule's findings?
  - what does the design phase mean and how do I claim fab-ready?
  - what do 'new / still / resolved' mean on a DRC run?
  - how do I declare pin electrical types and rail/voltage bounds for ERC?
  - view='drc' reports synthesized_footprint — how do I fix it?
  - how do I rename a wrong pin or annotate pin semantics (op='pin_rename'/'pins')?
applies-to: get/put (kind='pcb')
status: active
tags: [design]
kinds: [pcb]
---

# precis-pcb-check-help — one check surface

`view='check'` is THE check surface; `view='drc'` stays as its preset —
same findings, same table.

Workspace `Board.check()` also runs the stdlib `precis_geom.pcb_motifs.MOTIFS`
registry at netlist time: `led_without_limit` retains its current-path rule;
`motif.buck` checks SW–inductor–output against the FB divider and FB resistance
to GND, or accepts direct FB output sensing on fixed-output bucks;
`motif.ldo` checks VIN/VOUT capacitors to GND. Eligibility reads
`pin_functions` or semantic pin labels (`SW`, `FB`, `VIN`, `VOUT`, `GND`);
unclassified parts are ineligible. Passive roles or R/C/L designators identify
bridges; multi-terminal passives use every distinct-net terminal pair.
Each `Motif` has a name, eligibility predicate and check callback;
findings name the violating pin and net. High-side gate checks are deferred.

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
- Four rules fire only on `profile='prototype'` boards (`prod` DRC
  output is unchanged — [[precis-pcb-help]]): `via_density` (warn — more
  vias in some 10×10mm window than the profile's per-mm² ceiling allows),
  `via_under_part` (info, with a count — vias sitting under a part's
  courtyard; a soft router-cost signal, never a keep-out), `fine_pitch_pad`
  (warn — a part's own pads closer than 0.5mm centre-to-centre, hard to
  hand-solder), `starved_thermal` (warn — a pour-connected pad whose
  thermal relief would leave fewer than 2 spokes stays solid instead;
  named so a hand-soldered pad that heat-sinks like a plane doesn't
  surprise anyone).

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
the head adds a `by rule:` tally (rule, count, severity) and a `by net:`
rollup — the top nets by finding count (each net counted once per finding
that names it, both nets of a pair finding), so the nets driving the
violation count are visible without re-aggregating the rows. Narrow the
table with `args={'severity': 'error'|'warn'|'todo', 'rule': 'via_pad_keepout'}`
(`rule` takes a comma list; an unknown rule or severity is refused with
the valid options). The head counts stay board-wide and a
`filtered: showing N of M` line says the table is cut.

No via may sit in a pad: a via whose annulus overlaps a solder pad's copper
is an error (`via_pad_keepout`, or `via_in_pad` for an imported or authored
same-net fixed via), because a via in a pad needs a filled and capped via,
a fab upgrade we do not buy. Fix an imported one at the source board.

An authored keepout binds: a `features[]` row `{'ftype':'keepout',
'geom':{'poly':[[x,y],…]}}` — optional `layers:['F.Cu',…]` (absent means
every copper layer), `reason` names it — makes a track, via or pour
inside the polygon on a scoped layer a `keepout_copper` error. Pads are
not flagged: a pad inside a keepout is the placer's miss, not a copper
defect.

The fab-export path (`view='gerber'`) always judges at `fab`: an
authored `phase='placed'` claim is a progress statement, not a licence
to ship — a routed board's clearance/connectivity errors still lead the
bundle's DRC banner.

## Fill in a missing footprint — `op='footprint'`

`view='drc'` reporting `synthesized_footprint` means a catalog `part=`
instance has no cached pad geometry — it was DRC'd at a fabricated bound,
not its real footprint, so every other finding on that refdes is a guess.
Pull the real one:

```python
get(kind="pcb", id="s", view="drc")  # -> synthesized_footprint: part U1
put(kind="pcb", id="s", args={"op": "footprint", "part": "C639448"})
get(kind="pcb", id="s", view="footprints")  # confirm U1's C-number is cached
```

`parts=[...]` pulls several C-numbers in one call; a failed pull reports
`error` in that part's own row instead of raising, so one bad C-number
doesn't lose the rest. `force=True` re-pulls even when already cached.

The real footprint always wins and is stored. Stored routed copper it now
collides with is ripped (`<net> ripped: <rule>` — there is no re-route
until the topo realiser lands); pad or placement collisions
are listed as `now visible (real footprint)` and stand until a re-place. The
cache is shared, so the reply also names other designs using the part.

**Pads are not pins.** A pin is an electrical name the netlist declares and
wires (`pins`/`connections`, pin swaps, `pin_to_net` by name); a pad is
copper the footprint owns, keyed by pad number. Once a real footprint is
cached, every one of its pads is on the board — rendered, gerber'd and
clearance-checked — whether or not a pin names
it: an NC lead, an unused corner land, a second tab the footprint numbers
like the first. A pad whose name matches a declared pin carries that pin's
net (two pads sharing one name are two lands of one pin, bonded inside the
part); any other pad is net-less copper nothing may route through. Pad
count therefore equals the footprint's pad count, however many nets touch
the part; `view='drc'` names an unwired pad it hits by its footprint label.

Since Slice 2 (pcb-lazy-netlist-and-checks.md) **every footprint pad is
also a declared pin**: `put` completes a component's pin list from the
cached footprint's `pin_map` (the reply's `pins_completed` counts them; a
re-`put` completes a part whose footprint arrived later). A declared pin
with no net and no marker is an `unconnected_pin` finding — `warn` while
the board is being worked, `error` at fab phase; `nc`/`unused` silence it,
`must_terminate` upgrades it to an error. A footprint pad no pin claims at
all is `undeclared_pad`, and a part whose footprint is not cached reports
NOT-CHECKED — never a clean pass.

`pin_name_mismatch` is a different finding: the footprint IS cached, but
a declared pin name matches none of its pads, so that pin still sits at a
synthesized bound. Re-pulling cannot fix it; rename the pin. A design
`put` and `view='footprints'` both list the declared names beside the
footprint's own (`U_TEMP: declared VDD; the footprint names ADD0, ALERT,
GND, SCL, SDA, V+`).

When the vendor has nothing for a C-number, author the footprint directly
instead — same pad shape as `footprints:[...]` in `put`'s design-authoring
args:

```python
put(
    kind="pcb", id="s",
    args={"op": "footprint", "part": "C999999",
          "footprint": {"pads": [{"pin": "1", "shape": "rect", "x": -0.5, "y": 0, "w": 0.6, "h": 0.8}, ...]}},
)
```

A footprint dict takes `name`, `pads`, `pin_map`, `courtyard`, `centroid`,
`note` and `holes`; any other key is refused. `holes` is a list of
non-plated standoff/peg holes `{x, y, dia_mm}` in footprint-local mm (same
frame as the pads): they move, rotate and flip with their part, and reach
the drill file and DRC at the part's current pose. A
`put footprints` entry replaces the whole footprint: omit `holes` (or the
courtyard) and the stored ones are cleared, so re-send them with every edit.

`view='footprints'` lists every catalog-part instance on the board
(refdes, C-number, cached?, source, pad count, authored `op='pad'`
overrides) — the read-side counterpart, and where to check before
trusting a `view='drc'` pass on that part.

## Fix a wrong pin name — `op='pin_rename'` / `op='pin_remove'`

A connection pin that is a pad NUMBER of a cached footprint is wired to
that pad's pin name. A pin already declared wrong:
`args={'op':'pin_rename','refdes':'U1','pin':'8','to':'GND'}` keeps its
connection; if `to` exists, `op='pin_remove'` the wrong pin (its
connection goes too) and re-put the connection.

## Annotate pin semantics on an existing part — `op='pins'`

A re-`put` never edits pins (create-or-extend only), so the pin facts the
ERC rules read — `etype`, `rail`, `max_voltage_v`, `vih`, `vil`,
`max_current_a`, `current_role` (`series`|`shunt`|`signal`), `nc`,
`unused`, `must_terminate`, `min_dielectric` — are annotated after the
fact, one refdes per call:

```python
put(kind="pcb", id="s", args={
    "op": "pins", "refdes": "U7", "source": "datasheet p.12",
    "pins": [{"name": "VDD", "etype": "power_in", "rail": "3v3",
              "max_voltage_v": 3.6, "max_current_a": 0.5},
             {"name": "SDA", "etype": "bidi", "rail": "3v3"}]})
```

`name` must be one of the component's real pins — a bad one refuses the
call and lists them. Re-sending the same values is a no-op
("already as declared"); `null` clears a field, `pad` is refused (pad
binding stays with canonical-name authoring). `source` says who or what
asserted the fact (a datasheet page, `import`, a hand check) and is
recorded per pin; the `args.source` above applies to pins without one.
Annotating `max_current_a` without `current_role` still checks it against
the net's `current` — `shunt` is the only exemption (a tap, not the
through-path). `min_dielectric` (`x7r`, `c0g`, …) on a `power_in` pin is
the datasheet's "CIN ≥ X7R"-style requirement: `passive_rating` holds
every capacitor on the pin's rail to it. The rating side needs no
annotation — a part carrying a C-number gets value/dielectric/rating
parsed from its catalog description, shown as `derived:` on `#REFDES`.

## Move or resize one pad on one placed part — `op='pad'`

A pad that needs a nudge or a new extent on ONE placed part (a vendor
land revision, a hand-solder enlargement) is a per-instance override,
not a footprint re-pull — the shared row is never written, so other
designs and other instances keep catalog geometry while this part's
rendered pads, DRC and gerber use the override.
`args={'op':'pad','refdes':'J1','pad':'3','dx':0.2,'dy':-0.1}` shifts the
pad's centre in footprint-local mm; `'w'`/`'h'` resize to an absolute
extent. `revert:true` drops that pad's override — without `pad`, all of
the part's. Judged like every inline edit: a delta that adds or deepens
an error stores nothing. `view='pinout'` marks each overridden pad
`authored pad override`; `view='footprints'` lists their numbers.
Adding/removing pads or holes and `drill` edits refuse with a `next=`
that names the tracking design note (slice-2+).
