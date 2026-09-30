# pcb EasyEDA round trip

**Status:** ends when a colleague's EasyEDA Pro board round-trips through
precis (import, re-route to a correct spec, export Pro opens and edits,
order) with every loss warned rather than silent. Today the `.epro2` READ
path is proven against Reto's real 140-component 4-layer board; the
Store-facing half, the re-route it exists to enable, and the write path
are unbuilt. Make the imported board routable first, then make it leave.
Collides with ewod-pcb on generator/DRC/realizer files:
sequence, do not merge.
gr457053 is closed: a re-`put` now patches `net_class`/`est_current_a`/
`width_mm`/`note` onto an existing net alongside the 0171 spec columns, so
the annotation step slice 1b feeds can correct a net's current, not just its
voltage.
**Last reviewed:** 2026-09-30 (pillar review same day added
pcb-keepout-does-not-bind and gr451356)
**Worktree:** `pcb-easyeda-round-trip`

## Do next

1. **backlog/pcb-epro-import.md** — slice 1b (ingest + `precis pcb
   import-epro`), then 1c (`--copper=fixed` + the retire surface, same slice
   or the user imports copper they cannot remove). Everything else consumes
   an imported board; until 1b lands the reader is dead code held open by
   `_KNOWN_UNWIRED` entries in tests/test_pcb_dead_exports.py, deleted
   together when it lands.
2. **backlog/pcb-keepout-does-not-bind.md** — this thread's own finding,
   filed 2026-09-30; a keepout imported from a real board has no
   enforcement path, so re-routing (Do-next 3) can silently violate an
   area the source board actually respected.
3. **backlog/pcb-missing-constraint-classes.md** §E-1 router half —
   realize/maze draw to per-net clearance and cannot express a pairwise
   term, so the router lays copper view='drc' only flags afterwards.
   Re-routing to a corrected spec is the reason for importing; outranks the
   export half for that reason, not cost. Touches realize/maze, which
   ewod-pcb's generator depends on.
4. **backlog/pcb-epro-export.md** — slice 2b only (the smallest file Pro
   opens). Until a human confirms Pro opens our file, every later export
   slice rests on an unverified premise. R1 closed: bottom-side parts in
   from the start.

## Horizon

Import, re-route, export is one arc; split at import/re-route vs
export/fab if this file outgrows itself.

1. **td458069** — a NETTED arc and an asymmetric bottom-side part. Reto
   supplied arc-y.epro2 on 2026-09-30, which closed the ARC record itself;
   what remains is verifying handedness on the way OUT, since its arcs
   carry no net and nothing yet exercises the mirror outbound. Gates 2c and
   2f only; blocks nothing in import.

2. **backlog/pcb-epro-export.md** slice 2c (copper) — waits on 2b and
   td458069's arc; the first export a colleague can inspect, traces
   clicking through to the right net.
3. **backlog/pcb-epro-export.md** slice 2d (pours) — waits on 2c; planes Pro
   re-pours on open, the difference between readable and editable.
4. **backlog/pcb-epro-export.md** slice 2e (silk + editable designators) —
   waits on 2d and SilkPlacement gaining x/y/angle; retypeable refdes text.
5. **backlog/pcb-epro-export.md** slice 2f (deferred set) — waits on 2b–2e;
   each of footprint documents, rule areas, mask openings, NPTH, teardrops
   either round-trips or becomes a warned drop.
6. **backlog/pcb-epro-export.md** round-trip test (0.5 um geometry equality)
   — waits on both halves; the only regression that catches EasyEDA moving
   the format.
7. **backlog/pcb-engine-plan.md** §5 — 2-layer and n-layer stackups; enqueue
   refuses len(stackup) != 4 while the processor already handles 2. Waits on
   nothing; removes the wall a colleague's 2-layer .epro2 hits at import.
8. **gr451356** — a side-insertion connector (USB, card edge) must sit on
   the board edge, and nothing in the imported part/footprint model
   records that; an imported board that re-routes could relocate one to
   an unbuildable position with no warning. Waits on nothing; cheap DRC
   half is buildable independent of the placer term.
9. **backlog/pcb-design-source-provenance.md** — waits on 1b's annotation
   step; datasheet-chunk citations behind each pin/net spec the re-route
   trusts.
10. **backlog/pcb-flexboard.md** — waits on 6; the flex half of "all of them".
11. **backlog/pcb-guided-place-route.md** slice 9 (JLCPCB ordering) — the
    workflow's endpoint; gated on a human granting Components/PCB scope in
    the JLCPCB Open API console.

## Parked

- **td458070** — what APWR and BPWR actually carry; unparks when 1b lands
  and the annotation step runs (an input, not a blocker).

## No action needed

- Spike items S2–S12 and R1 in backlog/pcb-epro-export.md — resolved
  2026-09-29 against the real board; R2 and R3 stay open inside items 4 and
  2.
