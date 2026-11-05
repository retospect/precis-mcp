# PCB kernel contracts

Extended inventories and rationale linked from the architecture summaries in
`src/precis/pcb/__init__.py` and `src/precis/pcb/topoplace/__init__.py`.
Each section retains its source module's detailed contract; update it with the
owning implementation. The module docstrings retain the API map and core why.

## Mechanical review

``pcb_review`` works the shipped ``pcb-tapeout`` and conditionally assigned
``pcb-mech-env`` judgment ledgers through the same checklist sync/status/write
path. Mechanical review is declaration-driven (flex process, fab features or
board metadata), so a plain rigid board gains no implicit mechanical gate.
Batch ``put(args={'meta': ...})`` and workspace ``Board(meta=...)`` validate
``coating``/``enclosure``/``environment`` and merge them into the board row
in the batch transaction. Reference metadata alone is insufficient because
checklist applicability and revisions read ``pcb_boards.meta``. Explicit
``coating='none'``, ``enclosure=False`` and ``environment='indoor'`` do not
activate the checklist; incomplete legacy declarations still do.
Mechanical verdicts cover the whole board and consume the ``pcb:mech-env``
input revision: pcb_graph omits fab polygons, copper and footprint geometry,
so its fingerprint alone cannot invalidate a mask/coverlay decision. Missing
geometry records ``not checkable (gr520329)``, never an inferred pass.

## Stored route status

Stored ``meta.last_route.warm_start`` records each group's method and
distance in mm for boards routed before the cut — matched roles also
carry `cost_mm`/`prior_cost_mm`, which `format_warm_start` renders into
`view='route-status'` (stored runs only).

## Footprint geometry

* **Parts + footprints** — :mod:`~precis.pcb.catalog` (jlcparts dump),
  :mod:`~precis.pcb.jlc_api` (live stock), :mod:`~precis.pcb.easyeda` +
  :mod:`~precis.pcb.footprint` (pad geometry), :mod:`~precis.pcb.padplace`
  (pads in board coordinates: both centres and pad axes follow mirror,
  then instance rotation; bottom pad angles subtract from the instance
  angle). Router size probes use that same side and angle so their
  dimension swaps agree with fabrication. Axis snapping bounds the
  extremity displacement by half the fabrication quantum. Drilled polygon
  origins are independent of their bbox centres; moving an origin can move
  a hole without moving any copper. Undrilled polygon normalization retains
  its bbox routing representative, since changing that point reroutes
  unchanged copper. Native hole offsets stay pad-local until
  placement and become explicit world drill centres. Both offline and
  stored models use the same deduplicated slot-aware drill extraction.
  Router contact bodies, via/escape siting and export share
  ``padplace.place_pad_drill`` for offset centres and slot ends; a filled
  copper envelope cannot stand in for a physical drill or prove a pour
  contact inside its void.
  Connectivity and router pour contact share ``clearance.region_touches_land``:
  straight lands clip to actual copper, curved lands retain analytic contact,
  and open bores use outward cuts so hole facets cannot prove a connection.
  Placed pads retain their authored axis even
  when their land is circular/custom or becomes an IR polygon. Converted
  rect/stadium lands retain their native analytic shape and board angle
  through the IR fabrication model, Gerber copper/mask/paste and Pro export;
  DRC must approve the boundary actually emitted, rather than an analytic
  substitute for an inscribed polygon. Relative drill/slot probes remain
  unquantized until final board translation to avoid double-rounding rings.
  Slot rings require copper around the midpoint as well as side-wall
  distance, while their authored open end caps remain permitted.
  Shared ``clearance.body_of`` geometry also supplies pour blockers,
  selection targets/extents, mechanical electrode windows and electrical
  contact. Polygon boolean cuts enclose curved lands with the kernel's
  outward polygon; contact and slot-cap rings remain analytic, so facets
  cannot erase a small real connection or reverse a near-floor verdict.
  Fab mask previews subtract round-aperture strokes as well as flashes
  and regions. Mechanical fingerprints include the native land angles.
  Silk and annular-ring checks consume physical copper boundaries, and
  archive verification compares those boundaries and hole/slot geometry
  so equal dimensions cannot hide a rotated or damaged land. Anneal and
  topo replay versions invalidate verdicts from the earlier geometry.
  :mod:`~precis.pcb.landpattern`
  (synthesized pads when no footprint is cached),
  :mod:`~precis.pcb.escape` (footprint-intrinsic escape precompute),
  :mod:`~precis.pcb.capabilities` (fab rules as versioned data),
  :mod:`~precis.pcb.datasheets` (the ``datasheet_pull`` trigger + read
  side) and :mod:`~precis.pcb.datasheet_facts` (the extracted pin table /
  required externals / conditional-table block on the part ref —
  ``get(kind='part', view='facts')`` reads it, the ``datasheet_facts``
  job writes it).

## Intake and pad contracts

Fresh local EasyEDA Pro intake retains accepted LINE/ARC records and ordinary
through vias as fixed source copper, so preview does not erase existing routing
(gr470192). This supersedes the older regenerate-only default, still available
as copper="none". Partial updates retain that older behavior: their intentionally
unchanged nets/outline cannot safely receive changed copper. Import and source
copper share one transaction; neither the reader nor preview invents routing.

The web selection adapter binds actual instance IDs and shown raw snapshots to
Gerber flash targets; refdes and footprint UUID/design-local key remain separate.
Unknown identity stays unavailable. Shared design chat proposes a single move;
explicit Apply uses the existing handler under the bounded cache/row guard and
rejects any generator, copper or annotation collateral. Replay-normalized IDs
and routing hashes were refused as viewer identity because they omit those facts.

Preview airwires reuse copper connectivity from the full model exported to
Gerber: a pending net can contain several already-connected pins, so its
airwires must join components rather than redraw a tree over every pin.
Bare Gerber callers without that proof conservatively retain all connections.
Instance pointer fallbacks follow the union of their represented artwork
layers; both selection overlays and legacy body handles need that visibility
because their hit targets sit outside the toggleable layer groups.

Internal :mod:`~precis.pcb.snapshot` captures raw relational routing inputs and
checkpoints for dev/test replay; its module docstring owns the replay rules.

Dependencies: shapely is imported at module top by ``drc``, ``generators``,
``gerber``, ``ir``, ``planes``, ``realize`` and ``tiling`` — it is a core
dependency (pyproject), not confined to the tiling pass any more.
``geom.py`` stays dependency-free by convention because its primitives
are closed-form, not because the package as a whole is. No GL, no
meshing, no embedder. The handler (:mod:`precis.handlers.pcb`) renders
results as TOON; this package owns the algorithms.

The route pitch cap retains two-thirds of clearance, bounded by
existing pitch floor. A one-third experiment gained one net on the faithful
dogfood replay (12 gains/11 losses, zero geometric DRC errors), but regressed
ESP32 reference seed5 and fab-film seeds2/3/4. The global refinement was withdrawn:
deterministic routing competition changes with the grid, so finer is not a
monotonic yield improvement. The measured fine-grid fixture remains an explicit
experiment, not a default or new API. Clearance/layers/fixed copper, all route/DRC
ratchets and search budgets stay authoritative; diagnostic parity is separate.

Pads are not pins (gr451276). A pin is a netlist name; a pad is footprint
copper keyed by pad number, carried as the IR's pad set
(``ir.FootprintPad``/``footprint_pad_set``, memoised on
``PcbIR.footprint_pads`` by ``session.apply_real_pin_offsets``). Pins join
pads by name, first pad per name supplying the pin's own position/size;
every other pad of a real footprint is emitted by ``realize.pads_for_ir``,
claimed on the router grid (``_footprint_pad_claims``, under its pin's net
or a per-land sentinel) and hulled by the courtyard, so pad count equals the
footprint's regardless of wiring. Pads were never promoted to pins because
pin ids carry pin swaps, net indexing and pin_to_net-by-name. Connectivity
unions the pads of one pin: the part bonds them internally, so a second lead
is not an island of its net. Alternatives refused: seeding pins from the
footprint (breaks pin identity), and net-less extra pads (made a pin's own
second land a foreign obstacle to its net, measured on GND).

Slice 2 of docs/backlog/pcb-lazy-netlist-and-checks.md then closed the other
direction: every footprint pad NAME is also a declared pin —
``_pcb_apply`` completes a component's pins from the cached footprint's
``pin_map`` (``store._pcb_ops._pcb_complete_footprint_pins``), so the
netlist can name every land while pads still outnumber pins wherever two
pads share one name. A declared pin with no net and no §1j marker is
``drc.check_unconnected_pins`` (warn while the board is worked, error at
the authored ``fab`` phase); a pad no pin claims at all is
``drc.check_undeclared_pads`` — and a part whose footprint is not cached
is reported NOT-CHECKED, never a clean pass. The marker vocabulary
(``nc``/``unused``/``must_terminate``, on ``pcb_pins.meta`` + the ``nc``
tag) is what keeps that complaint an oracle and not noise.

Instance pinout inspection (``eyes.pinout``) reads raw stored pads and
reuses ``padplace.place_pad_point``. IR/fab pads were refused as its input:
synthesized bounds cannot establish numbering, and per-layer flashes
multiply through-hole lands. Explicit pin-to-pad and footprint naming are
reported separately rather than changing routing's mapping policy. Missing
placement leaves board coordinates unavailable; source geometry does not
verify a vendor pinout or connector mating orientation.

Unsaved intake preview overlays explicit proposed labels on those same stored
rows, separately from persisted pin/net evidence. It never authors the draft:
canonical ownership conflicts remain inspectable proposals, while authoring
still refuses them. Prose/numbering/mating/net inference and provider fetches
were refused; a read-only echo is not a routing or alias-storage policy.

## Shove and branch identity

The occupancy witness and realiser share ``realise._repair_bigon``:
an empty 2D bigon certifies a probe only when the emitter can exchange
its arcs without moving planted vias, changing layers or increasing
foreign-pad overlap. Keeping those refusals together prevents a new
emitter guard from silently reopening probe adoption.

The shove uses that witness for acceptance, saved crossing keys and blocking
partners. Same-net contacts are legal. A swap compares foreign-crossing sets
on untouched nets near the changed geometry, using a shared spatial index;
even a failing net cannot acquire a new short. Accepted swaps and withdrawals
refresh nearby nets' keys. Multi-start winners use the actual layer plan's
failure set rather than saved crossing keys: strict subsets win, equal sets
use copper costs, and incomparable sets keep the earlier incumbent. Dropped
via joins remain failures in both attempt and committed-state metrics. Swaps,
direct regrows, partial-copper withdrawals and order-loop retries also run
the final shaped-copper clearance guard, in the
candidate's final branch order, with board masks and filtered foreign-net
rules preserved.
The guard computes the full set because shaping and barrel clearance can
affect a net whose taut centreline never meets the changed wire; crossing-only
spatial culling would miss that failure. Changed nets must clear the guard and
untouched nets cannot gain guarded branches, including on an already guarded
net. Guard offences use concrete planned-descendant identities because another
net's withdrawal shifts plan indices and layer planning can replace a tree.
The input WirePaths seed those identities; flips and shaping retain them.
A new rethread branch gets its own WirePath identity, while a retained branch
keeps its existing one. Retention compares the actual resolved runs/cuts on
both sides of the splice. Re-reading nominal input layers loses corridor
legalization and can reject an unchanged descendant whose rebuilt annotation
names the resolved layer. Implicit and explicit annotations are equivalent
only when they resolve to the same physical layer state. Exact
corridor, endpoints, copper points, layer transitions and planted vias still
distinguish descendants. Ordinary crossing parameters may normalize without
moving copper; an unplanted layer join retains its physical crossing site.
The matching key never replaces the identity carried through earlier flips.
Input ancestry remains a separate,
transitive union of the replaced tree's ancestors. Using that union as failure
identity hid a second lost descendant with the same ancestors and falsely
rejected unchanged copper when its ancestry expanded. Both maps include
discarded branches and concatenate with the full multipass layer index space.
Branches lost to missing vias or planned crossings
also count, since they disappear before the clearance guard. Refused
withdrawals retain their original copper:
removal can change shaping and expose a short between surviving branches.
Unnamed snapshots share the same index-name fallback for pad ownership and
branch rules, so their prefix retries still reach the physical guard.
Order-loop retries retain the connected-partner requirement: a prefix guard
cannot predict which existing offence later ordinary grows would heal.
Rescuing a still-disconnected partner is reserved for the complete-state shove.
The prefix loop also keeps encounter-order blocker ties: changing an earlier
retry can alter an untouched net's later growth, beyond a prefix guard's view.
Beam keeps its best completed state
when a later round stalls. Withdrawing refused partial copper
leaves an ``Unreachable`` row
over all of the net's pins, so the realiser and gauge still count the failure.
Multi-start records accepted counts per start and round; no acceptance in the
first start skips the shuffled starts only after every failed net was tried
completely and no copper was stripped, while retaining the count budget.

## Plane escapes and copper obstacles

Plane-pad escapes run after signals so their copper cannot change signal
corridors. A reused barrel must span the stub and the independently joined
pour layer; final pours are checked again against pre-escape primitives,
including when every fragment vanished, so escapes cannot certify each
other. The length cap belongs inside candidate verification so a long
reuse cannot hide a shorter fresh site. Once a legal over-cap candidate
has supplied the diagnostic length, a conservative pad-radius bound cuts
off portals no under-cap stub can reach. Interval reachability samples
midpoints and quarters first, adding inset endpoints only when that search
fails, so an existing successful corridor retains priority over a new path
that could obstruct a later pad. Only after all five witnesses fail does
the search subtract analytic obstacle shadows for that portal pair.
Fixed-source subtraction is exact; two-interval subtraction may admit a
multi-obstacle split, so complete-polyline verification still precedes
emission. Keeping shadow work local avoids the cost of subtracting shadows
for every successful transition.
Whole-stub clearance checks select nearby copper through a spatial index,
then apply the same exact pairwise clearance predicates; this keeps
continued candidate verification affordable without weakening its rules.

Route obstacles enclose their true copper: authored arcs and fixed vias
use the clearance kernel's outward polygons, as does frozen pass-2 copper;
round-pad obstacles use circumscribed circles. Chords inside the copper
otherwise make a taut wire appear legal inside the net's clearance band.
Named fixed copper/vias retain their net's clearance through the planter,
occupancy, shaping indices and final guard, so foreign pairs always owe
the larger rule, including two planted vias. Keepouts carry no net rule
or ownership exemption, even when supplied with a matching net name.
Plane fanout sites index each earlier stub and barrel with its net rule.
Pre-route stubs retain the conservative all-layer zone veto: admitting a
new frozen stub can obstruct an otherwise clean signal corridor. Deferred
escapes check only their actual layer after signals have settled. Authored
barrels block new fanout drills even on their own net.
A final guard checks all plane nets against the complete signal/fanout
set before appending copper. Escape polylines and fresh barrels use the
same CopperIndex oracle. A legal barrel site can require a blocked stub,
so rejection retains the portal's remaining certified points in their
existing order. They are tried after the established single-site search
and endpoint fallback exhaust across every barrel size, preserving
successful fanouts' room for later pads. The planter caches the full
surviving set by dynamic-index identity and mutation version; its
single-point callers keep the same first choice. A reused barrel emits
only a new stub, so its authored geometry is not re-priced as a new drill.
When every portal search and barrel size fails, up to 64 nearest outward
zone-boundary proposals per size can reach a short direct fresh site inside
a triangle. The transformed planter bodies supply those proposals; shared
pad/web, environment, physical-drill and dynamic predicates certify the
center before the same whole-stub/join/cap verifier runs. Buffers propose
only, so overlapping zones still reject. This bounded fallback preserves
all established successes and makes no claim of complete interior search.
Pour joins read exact pad bodies, since an outward obstacle ring can
certify copper in air.
Stadium cores retain their exact swept radius. Raw footprint geometry
travels with the design snapshot because oblique extent boxes lose the
pad's rotation; unresolved shapes block routing but cannot certify joins.
Track joins use the whole swept body, including arcs, and via joins use
the annulus outside the drill void. Fanout siting and the final guard
check complete stub and barrel clearance against the outline boundary;
checking only the barrel centre misses tracks across concave notches.
Wire-tree searches cache interval midpoints for their own lifetime: the
occupancy snapshot is fixed during a search, and repeated coordinate
construction was a hot path after merging the clearance oracle.

## Ownership and fabrication reserve

``TopoDesign.pin_nets`` resolves pad names from the full IR table and
ownership through the routed name table: singleton/plane filtering changes
branch indices, never pad rules. Reassignment retains both named/static
rules and the clearance of peer vias. Mesh pad polygons are clearance
envelopes too; exact ``_pad_bodies`` alone certify connectivity,
so an outward polygon cannot invent a copper join. Fixed copper/vias' owners
may cross their grown rings, keeping authored vias reachable on their own net.
Per-piece coalescing drops redundant collinear crossings before repair:
their arbitrary sampling points must not pin a leg inside a peer's
clearance band. Pad/via joins remain the piece's untouched endpoints.
Emission serializes the guarded points verbatim: micro-coalescing can
change geometry on a second pass and must not run after validation.
The worker preserves validated copper coordinates through persistence:
rounding a via site can consume its remaining mask web after the guard.
The local copper JSON artifact preserves them too. Placement, route, escape
and fanout oracles reserve ``clearance.FAB_QUANTUM_MM`` beyond the exact rule,
including mask web, edge and drill-wall checks: two objects' 0.0001 mm
export quantization can consume 0.000213 mm in relative position/size.
The 0.000225 mm reserve covers that loss without weakening DRC. EasyEDA
footprint identity uses 0.000001 mm precision so near-equal pads cannot
substitute geometry larger than this reserve.
Post-route widening also keeps the reserve: only the trial net's diagnostic
track radii grow for its exact DRC check; accepted widths stay unchanged.
Bend spacing judges the complete shared-vertex fan against its peers;
sequential admission can reject legal collective moves against unmoved
peers. It must preserve proper crossing counts: adding an empty bigon
after layer assignment can short two wires whose earlier crossing used
different layers. A missed thin peer supplies an exact repair point when
the emitter's sampling grid finds none; failed exact-point detours retry
the previous midpoint candidate, which may fit where an endpoint kink cannot.
Placement keeps the reserve in pair, zone, hole and outline checks, with
the same zero-round audit used by legalize and shove. Same-net authored
via exemptions still test the original copper geometry.
