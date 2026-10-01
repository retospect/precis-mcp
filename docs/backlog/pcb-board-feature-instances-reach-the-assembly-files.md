---
status: draft
title: A generator's board-feature instance gets a pick-and-place line, because nothing marks it as not-a-component
prio: normal
pillar: 3d-design
---

# The CPL tells the assembler to place the board's own copper

Dogfooded on prod, `ewod-dogfood-6`, 2026-10-01.

`view='cpl'` emits:

```
Designator,Mid X,Mid Y,Layer,Rotation
ARR1,0.0000,0.0000,Top,0
ARR1_SINK_0,10.0000,10.0000,Bottom,90
```

`ARR1_SINK_0` is a real driver IC and belongs there. **`ARR1` is the EWOD
electrode array** — 55 electrode pads that are etched board copper, authored
by the `ewod_pad_array` generator. There is no component to pick and nothing
to place. A CPL is consumed by an assembly machine, so this line is at best
ignored and at worst an assembly error or a quote rejection.

`view='bom'` has the same root cause but is at least audible about it:

```
Comment,Designator,Footprint,LCSC Part #
EWOD pad array (8x8 full),ARR1,__gen_ARR1,
...
⚠️  1 part(s) without an LCSC number (not JLCPCB-assemblable): ARR1
```

Note the framing that warning is forced into: it describes a *board feature*
as a *deficient part*. "Not JLCPCB-assemblable" is true but beside the
point — `ARR1` is not a part at all. `cpl_csv` has no equivalent warning
path and emits the row silently.

## Motivation / why

`export.cpl_csv` iterates `model["instances"]` and filters on exactly one
thing: whether the instance has coordinates ("Unplaced instances (no x/y)
are skipped — the caller flags them"). There is no notion of an instance
that is placed but is **not an assembled component**, so a generator that
models authored copper as an instance — which is the right way to give it a
refdes, pins, nets and a pose — necessarily leaks into the assembly files.

The information to exclude it already exists at the row: `ARR1` carries
`roles: ['ewod_array']`, a NULL `part_lcsc`, and a generator-owned local
footprint (`__gen_ARR1`). What is missing is the *concept*.

## In scope

A general "this instance is not an assembled component" fact, and the two
exporters honouring it.

## Explicitly NOT in scope

- **Filtering on "has no LCSC number".** That is the tempting one-line fix
  and it is wrong: a hand-soldered connector, a mechanical standoff, or a
  part sourced outside JLCPCB is a real component with no LCSC number, and
  dropping it from the CPL/BOM would silently lose a part someone must
  actually fit. Absence of a catalog number is a sourcing fact, not an
  assembly fact.
- **Filtering on `roles` containing `ewod_array`.** Narrow and safe, but it
  encodes one board's vocabulary into the exporters, against the standing
  rule that the code supports arbitrary requests rather than this board. The
  next generator that authors copper as an instance would need another
  special case.
- Changing the DRC/export relationship — `pcb-tapeout-checklist-seed-items.md`
  owns the "should an export be gated at all" question.

## Acceptance criteria

- A generator-authored board-feature instance appears in neither the CPL nor
  the BOM, while every real component — **including one with no LCSC
  number** — still appears in both. The negative control is the point here:
  a fix that drops LCSC-less parts passes a naive test and breaks real
  boards.
- The BOM's "without an LCSC number" warning no longer names board features,
  so when it does fire it means what it says: a real part someone has to
  source.
- `view='mechanical'` is unaffected — a board feature's geometry is
  legitimately mechanical, and that export is the 0041 bridge, not an
  assembly file.

## Target + blast radius

`src/precis/pcb/export.py` (`cpl_csv`, the BOM writer and its warning),
whatever carries the new fact (likely `pcb_instances.meta` or a role
convention — `pcb-component-model.md` owns the Component/LandPattern/
Instance reframe this belongs inside, so sequence with it rather than
inventing a parallel flag), and `src/precis/handlers/pcb.py` where the
warning text is composed. Changes the CPL/BOM content of any board carrying
a generator instance.

## Open questions / decisions log

- Is the fact a property of the INSTANCE (this one is virtual) or of the
  COMPONENT (this kind of thing is never assembled)? Component-level is
  tidier and matches `pcb-component-model.md`'s grain, but a generator mints
  its component on the fly, so instance-level may be the only place it can
  be written today.
