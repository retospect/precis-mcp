---
status: draft
---
# Topo placement access — establish the diagnostic first

Slice P in [topo-route-geometry-truth](topo-route-geometry-truth.md) is
independent of signal shove. It must automatically avoid the named
keepout-walled nets and trapped plane pads without hand-editing poses.

## P0: distinguish static access from signal competition

Build a local diagnostic on a pinned source snapshot and unchanged board
fixtures. For each named signal net, retain the board's complete geometry,
pad ownership, fixed copper, labels, keepouts and allowed layers, but enable
only that net's terminals and start with empty negotiated signal occupancy.
Use the existing mesh and wire-search predicates. Report the returned
reachability/failure and wall owners; a failed heuristic search is not proof
that no geometric route exists.

For the named ground pads, record the existing fanout and deferred-escape
outcomes, including candidate rejection reasons. Separate authored obstacles
from generated fanout copper. Do not introduce a second clearance model or
classify every missing via as a placement failure.

The report must identify the source, fixture and rule hashes; enabled nets;
permitted layers; preserved obstacle policy; and exact queries made. Establish
that unrelated signal nets were not routed during input construction. Run a
small synthetic smoke first. Private-board queries wait until the active S2
measurement worker finishes. No full forty-row matrix is needed for P0.

## P1: bounded automatic repair experiment

P0's pinned queries return four isolated signal failures and three missing
plane escapes even without generated fanout obstacles. Prototype an automatic
translation repair using those same predicates. Derive candidate part owners
from stranded terminals/missing pads, not a table of hand-selected positions.

Generate deterministic translation candidates using the existing placer
domain/step conventions and obstacle geometry. Respect fixed positions,
freedoms, leashes and authored hard measures. Rebuild pose-dependent pads,
owned keepouts, labels, fanout and meshes for every candidate; stale geometry
cannot certify a moved pose. Keep all changes in memory.

Rank by missing target terminals plus missing target plane pads, then total
displacement. An accepted move must reduce failures and preserve previously
reachable terminals and plane pads, including surviving terminals on already
failed nets. Reject new geometry/hard-measure violations. Use existing
predicates and measurement helpers, not a second physical model.

Limit proposals and kernel queries explicitly. Compare baseline/best result
with exact poses and receipts. If no legal improving move is found, retain the
baseline and report the limitation. This bounded experiment is not a proof
of impossibility, a production placer change or full-board routing acceptance.
Private probes run under the shared heavy-validation lock.

## Production design follows the experiment

P1 found no legal improvement in either fixture within 48 proposals. On
local-66, sorting short translations first used the entire proposal budget
on moves no larger than 1 mm; larger scales existed but were never sampled.
The other fixture sampled larger moves without target recovery. Baseline
hard-measure violations remain, so retaining the baseline is not proof of a
legal placement.

The next experiment must reserve proposal coverage across distance bands and
obstacle-clearing candidates, report the constraints rejecting moves, and
compare a failed endpoint with a reachable counterpart where available.
Trace the actual moved pad and obstacle coordinates. Determine whether coupled
constraint-aware moves are required before adding a production proxy term.
Increasing the same short-move budget alone is not the next design.

P2 keeps the 48-proposal/240-query caps per board. Allocate actual unique
translations across owners, available displacement bands and obstacle-clearing
families before filling extra short-step slots. Classify bands after clamping;
large nominal moves clamped to tiny shifts do not establish coverage. Add
nearest-boundary proposals as candidate generation only; authoritative checks
remain unchanged. Record available/selected families, actual displacements,
duplicate elimination and every rejecting constraint. Compare failed x2
access with a reachable counterpart where available. No hardcoded replacement
poses or constraint relaxation; publish unchanged results if no move improves.

The main loop selects a placement term/check from that evidence. It must
reuse current physical predicates, respect locks and authored hard measures,
and price or repair the named failures automatically. Full routing on every
anneal move is not presumed affordable. Incremental scoring must agree with
full recomputation if a new proxy term is selected.

Final P acceptance remains the owning spec's twenty corridor-limit instances
to zero and three trapped ground pads to zero, with legal poses and unchanged
hard-measure requirements. P0 is evidence preparation, not that acceptance.
