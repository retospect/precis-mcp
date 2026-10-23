---
status: draft
title: Local edits on processed designs — identity-targeted steps, local repair, a versioned record
pillar: 3d-design
prio: high
---

# Local edits on processed designs

Seeded from [dev-loop-scratch](dev-loop-scratch.md) §D3 (the 2026-10-23
discussion). A processed design — a relaxed structure, a routed board, a
checked CAD body — is the expensive product of the workspace pipeline.
Today the only sanctioned way to change one is to rebuild the recipe
end-to-end (or mutate through an op and lose the record). An edit should
be a small step on top of the stored version: select a target by name,
repair only what the edit touched, verify, and keep a provenance record —
not a full relax, route or rebuild.

The pieces were ruled separately on 2026-10-23 and this spec collects
them into one edit loop:

- `build(recipe)` is a step stack keyed
  `hash(step name, params, parent keys)` — changing step `k` rebuilds `k`
  and its descendants, everything upstream is a cache hit
  ([se-flow-kernel](se-flow-kernel.md) §Build-step stack cache).
- Assembly relax already repairs locally: free region is a buffer around
  what the join frames touched, frozen interiors, a residual-force check
  that widens the shell, full relax warm-started only on failure
  ([structure-workspaces](structure-workspaces.md) §Relax hierarchy (c)).
- PCB hierarchical routing already rips up and re-routes only
  frame-scoped nets, keeps the rest locked, then runs the global pass
  and DRC ([pcb-workspace-code](pcb-workspace-code.md) §Modules, slice 3).

## Motivation / why

Reto 2026-10-23: an edit is a build step (swap H→F, fillet, hole, move a
part) that selects its target by stable identity (atom uid, refdes,
named face), never by index, on top of a stored version. Everything
upstream of the step is a cache hit. Relax and routing are non-local
only in the physics sense — a small edit perturbs a small neighbourhood,
so paying full-relax or full-route cost for an H→F swap or a moved
resistor throws away exactly the structure the versioned pipeline
exists to keep. The record matters as much as the edit: without the
parent link, the step, the repair record and a diff, an edited design
is indistinguishable from a freshly built one and "why does this look
different" has no answer.

## In scope

- **The edit step.** One shape at every scale: `{op, params, selection}`
  applied on top of a stored version. The selection names targets by
  stable identity — atom uid, refdes (incl. the module form `U2.CIN`),
  named face/feature — never by position or index. Examples it must
  cover: substitute H→F on a set of atoms, move a part, drill a hole,
  add a fillet, change a dimension.
- **Local repair, verify, widen only on failure** — one rule, three
  repair paths:
  - **atoms:** a buffer relax around the edit's touched atoms starting
    from the stored relaxed geometry; a single-point residual-force
    check on the frozen atoms; a full relax warm-started from the
    buffered geometry only when the check still fails after widening
    (the §Relax hierarchy (c) machinery, reused — not a second
    implementation).
  - **PCB:** rip up and re-route only the nets the edit touches, all
    other copper locked, then DRC (the hierarchical pass machinery).
  - **CAD:** rebuild the step stack under the edit's new key, then DRC
    and mould checks scoped to the faces the changed steps touch.
- **The record.** Each edit produces a new immutable version whose
  parent is the edited version, carrying: the step (op, params,
  selection, script sha or author), the repair record (buffer radius or
  net set, touched count, residual-force or DRC verdict, steps taken)
  and a diff view against the parent.
- **The identity prerequisite.** Selection-by-identity only works if
  identity survives relax and routing: an atom uid must name the same
  atom after a relax writes geometry back, a refdes/net must mean the
  same thing after a scoped reroute, a named face must survive a step
  rebuild. Tested with `jiggle` (dev-loop-scratch D0) before any edit
  path is built on it.

## Explicitly NOT in scope

- Full relax, full route or full rebuild as the default path — each is
  the fallback a failed verification escalates to, not the product.
- New physics, new relax rungs, new DRC or mould checks — the paths
  reuse what exists.
- History browsing, undo UI or branching beyond the parent chain and the
  parent diff — versions form a chain, not a tree browser.
- The construction-op retirement timeline (W5 in
  [structure-workspaces](structure-workspaces.md)) — whether an edit is
  an MCP op, a script-side step kind, or both is an open question below,
  not decided here.
- Interactive or GUI editing; edits enter through script or MCP call.

## Slices

Draft shape — order follows the prerequisite, scale order follows the
dev loop's atoms → PCB → macro (Reto 2026-10-23).

| Slice | What | Acceptance |
|---|---|---|
| E0 | **Identity substrate.** Atom uids stable across relax write-back; refdes/net identity stable across a scoped reroute; named-face (or frame-id) selection on the cad graph. `jiggle` supplies the relax mover for the test. | a `jiggle` relax returns the input uid set unchanged; a scoped reroute leaves untouched nets' identity intact; a rebuild keeps the named selection resolvable |
| E1 | **Atoms edit + buffer repair.** The substitution/move edit step, buffer relax from stored geometry, residual-force check with shell widening, warm-started full relax on failure, the version record and the parent diff. | an H→F edit on a stored relaxed block produces a new version whose repair record names radius, touched count, residual verdict and steps; a seeded case that fails the check escalates to a warm-started full relax and the record says so |
| E2 | **PCB edit + scoped reroute.** Part move / footprint swap / pin change, rip-up and reroute of touching nets only, DRC, the version record and the parent diff. | moving one part reroutes only its nets, the echo names them, untouched copper is byte-identical, the version's repair record carries the DRC verdict |
| E3 | **CAD edit + scoped checks.** A param/feature edit through the step stack, DRC + mould checks on affected faces only, the version record and the parent diff. | editing a hole diameter rebuilds only the hole step and descendants, checks run only on the affected faces, the diff view shows the change against the parent |

## Acceptance criteria

Draft-level; tightened when the spec goes `ready`.

- An edit never re-pays upstream cost: for each scale, a one-target
  edit on a stored version completes with only the scoped repair run
  (buffer relax, scoped reroute or affected-face checks), verified by
  the repair record.
- Every edit produces a version with a parent, the step record, the
  repair record and a working diff view against the parent — the four
  record parts are all present or the edit did not happen.
- Identity survives: the E0 acceptance holds on `jiggle` before E1–E3
  build on it.
- Widening is on failure only: the happy path of each scale runs no
  full relax / full route / full rebuild.

## Target + blast radius

- Atoms: `precis/structure/relax.py` (relax dispatch + the fragment
  fixed-mask machinery in `precis_se/atomic/relax_block.py`), relax
  write-back wherever atom identity is assigned, the se version/store
  path (`precis_se/persist.py`).
- PCB: the scoped place/route machinery (pcb slice 3's frame-scoped
  seed + global pass, `pcb_requests`), DRC findings.
- CAD: `precis/cad/graph.py` step/feature identity, the DRC/mould check
  entry points.
- Shared: the trace schema (the step record rides creator frames), the
  version/parent store, one diff renderer per scale (possibly D2's
  trace views).

## Open questions / decisions log

1. **Identity representation per scale.** atoms: do uids survive the
   relax write-back path today (geometry comes back as a new object —
   is the uid carried, re-derived, or lost)? pcb: refdes and net names
   are stable, but does copper/track identity survive a scoped
   reroute, and does it need to? cad: the gr506996 ruling gives DSL
   text with a frame id per node — is a "named face" the frame id, a
   new name layer on the graph, or a port?
2. **Where the version lives.** se persist versions are addressable
   already; is an edit a new design version, a per-block structure
   version, or a `design_revisions` row (the git-objects option in
   structure-workspaces §Code storage leaves this open)? Parent link:
   a column, or the existing `derived-from` edge?
3. **Edit surface.** MCP op(s), a `precis_geom` step kind in the build
   stack, or both under the ops/scripts DRY rule? If an op, does it
   become the surviving shape the W5 retirement leaves behind for
   `set_pose`-like mutation?
4. **Widen policy per scale.** atoms widen the shell 2 Å ×3 then go
   full — settled by §Relax hierarchy (c). pcb: on a failed scoped
   reroute, unlock adjacent nets then fall back to a full route, or
   refuse with `next=`? cad: is "affected faces" exactly the edited
   step's dependency cone, or also faces adjacent in the check sense?
5. **Diff view.** one renderer per scale (atom-set + displacement;
   net/track/part sets; feature/face sets), or one renderer fed by
   three serializers — and does D2's trace view carry it?
6. **Selection grammar.** pcb views already take `@net`, `#refdes`,
   `frame:<key>` — one shared handle grammar across scales, or
   per-scale selectors?
7. **Step record shape.** does the edit record reuse
   `trace_schema.frame_key` and creator frames (so edits appear in
   trace views and "who made it" answers uniformly), with the op-path
   precedent (`tool_ledger` call id as `line`) covering MCP-driven
   edits?
8. **Dependency on the dev loop.** E0's test harness is `jiggle`
   (dev-loop-scratch D0); does the spec block on D0 landing, or can E0
   run against `emt`/fixture movers until `jiggle` exists?
