---
status: ready
title: LLM-guided topological place+route for the pcb kind (sketch-as-canonical)
prio: high
model: opus
---

# LLM-guided topological place+route for the pcb kind (sketch-as-canonical)

## Motivation / why

The v1 routing story (export a Specctra DSN, run the Freerouting jar, parse
the SES back) treats routing as an opaque rented kernel: when it fails, the
LLM learns "12 unrouted" and nothing actionable, so the reroute→replace
back-edge of the design loop has no information flowing through it. The
pcb kind's whole thesis is that the LLM reads design state as a legible
graph — routing is the one stage that still isn't.

This spec replaces the *core* routing/placement story with an in-house,
LLM-guided **topological (rubber-band sketch) router** plus a plane-aware
placer, on the Maley/Dai two-stage architecture (TopoR's lineage):

1. **Sketch** — per connection, the purely combinatorial choice of which
   side of each obstacle (pin cluster, via, board edge) the connection
   passes. Small, discrete, diffable — the LLM can read and patch it.
2. **Realize** — deterministic geometry: turn sketches into arcs/segments,
   with clearances computed from how many connections squeeze through each
   gap. Failures are *legible*: "bundle B7 cannot pass between U3 pad-14
   and via cluster V22, gap 0.31 mm, needs 0.55 mm".

**Sketch is canonical; copper is derived.** Realized tracks/vias are a
regenerable artifact of (sketch + placement + rules) — the same
cascade discipline as chunks→embeddings. Move a component and the sketch
usually survives; only realization reruns. Rip-up = edit a sketch row.

Division of labor (the existing eyes philosophy, extended): deterministic
code owns realization, clearance math, DRC, plane polygon ops, congestion
accounting. The LLM owns strategy: net classes and routing order, which
bundle to rip when the realizer reports a squeeze, placement moves,
plane assignment, phase back-edges. Every automatic tool must **fail
legibly** — numbers and digests, never bare success/failure.

A default **4-layer stackup (SIG/GND/PWR/SIG) simplifies the problem**:
power/gnd pins become "drop a via to the plane", removing ~⅓–½ of the
ratsnest from both the placement objective and the router.

Compute model: **Postgres is checkpoint and truth; RAM is the workspace.**
Boards in our class (≤ ~500 instances, ≤ ~1500 nets, ≤ ~10k pads) hydrate
in one query into an in-memory model (shapely STRtree + numpy + plain
graphs); no SQL inside optimization loops; only committed results write
back. Long compute (autoplace, autoroute) runs as worker jobs on the
existing job substrate — never inline in an MCP tool call (the serve
thread-pool starvation lesson). Quick analyses (DRC, ratsnest, single-net
ops) stay inline.

Four cheap representation hedges are bought *now* because they are brutal
to retrofit against a design corpus (§Schema): netlist≠board (multi-board
systems), stackup-as-data (flex / aluminum / rigid-flex), fold lines as
geometry with 3D derived into the `cad` kind, and domain-typed nets with
class-driven router rules (microfluidic / thermal co-design). The hedges
are schema shape only — v1 behavior stays one rigid 4-layer electrical
board.

## In scope

### Schema (one forward-only migration, 0138 — verify against the prod
ledger before shipping; renumber if a sibling lands first)

- **`pcb_boards`** — board_id, ref_id, name, `stackup jsonb` (ordered
  layer array: `[{name, role: signal|plane|dielectric|stiffener, material,
  thickness_mm, plane_net?}]`; default template = 4-layer rigid FR-4
  SIG/GND/PWR/SIG), `fold_lines jsonb` (empty in v1), outline stays a
  `pcb_features` row now keyed by board. Backfill: one board per existing
  design.
- **`board_id`** added to `pcb_instances` and `pcb_features` (FK →
  pcb_boards; NOT NULL after backfill). Nets and netconns get **no**
  board column — the netlist layer never references geometry; a net spans
  boards through connector mate links (`meta.mates_with` on instances in
  v1; a first-class link kind only when multi-board work actually starts).
- **`pcb_nets.domain`** — text NOT NULL DEFAULT 'electrical', CHECK in
  ('electrical','fluidic','thermal'). v1 rejects non-electrical at the
  handler with a clear message.
- **`pcb_net_classes`** — per design: name (joins `pcb_nets.net_class`),
  `rules jsonb` (clearance_mm, track width, via drill/annular, permitted
  layers, length-match group, domain defaults). Missing row ⇒ built-in
  defaults. The router/DRC read rules *only* from class data — no copper
  assumptions in the core.
- **`pcb_routes`** — the canonical sketch: (board_id, net_id), `tree
  jsonb` (the net's two-pin connection decomposition incl. Steiner/via
  points), `topology jsonb` (per-connection ordered (anchor, side) list),
  `layer_assign jsonb`, `status` (unrouted|sketched|realized|failed),
  `fail jsonb` (the legible failure: blocking gap, participants,
  clearance arithmetic), updated_at. UNIQUE (board_id, net_id).
- **`pcb_copper`** — derived, regenerable: ctype (track|via|pour),
  layer, net_id, route_id?, `geom jsonb` (polyline+width | via
  pos/drill | pour polygon), `generated_at`. Regenerated wholesale per
  realize run (DELETE board's derived rows + INSERT — mirrors the
  chunks discipline; never hand-edited).
- **`pcb_planes`** — authored plane assignment per (board, layer,
  net) + `region_hint jsonb`; derived polygon + island report live in
  pcb_copper (ctype=pour) + DRC findings.
- **`pcb_drc_findings`** — durable, linkable: (board_id, run_id, rule,
  severity error|warn, objects jsonb, detail text, waived_by?). Gate
  evaluators and the LLM read the latest run.

### Footprint reality (unblocks everything geometric)

- **Parametric generator first**: IPC-7351-ish generated footprints
  (pads + courtyard + centroid + pin-map) for the standard package
  families — chip R/C (0402/0603/0805…), SOT-23/89/223, SOIC, TSSOP,
  QFN/DFN, SOT/TO powers, 2.54/1.27 headers — keyed by `parts.package` +
  params. Deterministic, offline, unit-tested against published IPC
  dimensions for a sample of each family.
- **Wire the easyeda2kicad conversion** (existing Slice 2 residual) as
  the long-tail fallback: EasyEDA API JSON → pads/pin_map/courtyard into
  `part_footprints`. Network-gated exactly like today; generator wins
  when both exist unless `meta.footprint_source='easyeda'`.

### Engines (all in `src/precis/pcb/`, pure-Python, unit-testable)

Dependency: **`shapely` becomes a core runtime dep** (pyproject `[project]`
deps, beside numpy which is already core) — DRC/plane reads run inline in
serve, so extras-gating would repeat the asa-venv silent-fail trap. Its use
is confined to drc.py / planes.py / realize.py; **`pcb/geom.py` keeps its
"no dependencies" convention untouched**. New-core-dep fallout applies:
precis-dev image rebuild (or the UV_WITH bridge until then) + serving
venvs pick it up via the normal deploy.

- **DRC engine** (`drc.py`): STRtree spatial index over pads/copper/
  features; class-rule clearances, width, annular ring, courtyard
  overlap, board-edge, plane-island connectivity, unconnected-item.
  Emits `pcb_drc_findings` rows + a TOON digest. Supersedes
  `eyes.drc_lite` behind the existing `view='drc'` (same address, real
  engine); eyes.py keeps ratsnest/crossings/proximity/measures. Plane/
  copper rules in slice 3 are unit-tested against **synthetic
  `pcb_planes`/`pcb_copper` fixture rows** — slice 3 does not wait on
  slices 5/7 for real data.
- **Plane segmentation** (`planes.py`): assign power nets to plane-layer
  regions (seeded by load-pin clusters), legalize with polygon ops,
  thermal reliefs, stitching-via proposal, island detection
  (plane polygon − antipads − splits → connected components). Plus the
  **signal-layer fill post-pass**: after routing, flood signal layers
  with gnd-tied pour (board − tracks − clearances), stitch to the plane,
  cull slivers/orphan islands (the island detector). Copper retention is
  a **derived post-pass, never an optimizer objective** (pour area is
  the complement of wire — a cost term would double-count); per-layer
  **copper fraction** reported as an advisory DRC metric (balance/warp).
- **Placer v2** (`place.py` extension): **simulated annealing over a
  constructive seed** (connectivity clustering + cluster drop; SA is
  the refiner — at ≤500 components it beats force-directed on rotation,
  locks, and arbitrary cost terms). Moves: translate, rotate 90°,
  swap-pair; `fixed='xy'|'rot'|'both'` = restricted move sets, locked
  parts still contribute cost. Cost = weighted legible terms:
  signal-net crossings (**plane-served nets excluded**) +
  **peak region utilization** from a RUDY-style grid estimator (net
  demand smeared over bounding boxes vs. per-region track capacity;
  penalize the peak, NOT variance — evenness emerges, clusters stay
  clustered) + region-priced **via demand** (gnd/power pins count as
  vias; a via blocks all layers, so its cost is the local congestion,
  not a scalar) + courtyard-overlap + measures as soft terms. Digest:
  per-term, per-region table + per-component move list; the LLM's
  lever is re-annealing from current state with adjusted weights/locks.
  Post-route, realized per-region density replaces the estimate (the
  6→5 back-edge carries real numbers; predicted-vs-realized per region
  is the estimator's calibration signal).
- **Topological router** (`sketch.py` + `realize.py`): net → two-pin
  connection tree; sketch search (side choices) minimizing crossings +
  congestion; deterministic realizer with per-gap capacity accounting;
  congestion digest per board region; rip-up primitives (rip one
  bundle/net, pin a topology choice, re-realize incrementally).
  Escalating-cost reroute (PathFinder-style negotiation) inside the
  autoroute job; the LLM intervenes between rounds, not per segment.
  Cost policy for plane-served nets: **never route ground/power beyond
  the dog-bone fanout** (pad → short stub → via to plane; via-in-pad
  wicks solder, so the stub is mandatory for SMT). Cost ordering:
  fill ≪ via (region-priced) ≪ routed gnd trace — a routed gnd trace
  on a signal layer is a last resort (pin can't reach its plane) and
  is flagged as an anomaly in the route digest.
- **Bundles**: nets sharing class + endpoints-neighborhood route as one
  sketch entity (buses); "move in bunches" falls out of bundle-level
  rip-up.

### Tool surface (existing four verbs; no new verbs)

- Reads extend the **existing `view=` mechanism** (`handlers/pcb.py`
  `_VIEWS` — no new addressing scheme): `view='drc'` is re-backed by the
  new engine; new views `'congestion'`, `'planes'`, `'route-status'`.
  Inline, TOON digests. `precis-pcb-help` updated in the same slice.
- `put(... args={op:'route', nets|bundles|all, …})`,
  `put(... args={op:'place', …})` — **enqueue worker jobs**; return job
  id; results land as digests + rows. Idempotent per (design, op,
  content-hash). The existing inline `args={'autoplace':…}` is
  **retired as an alias**: it enqueues the same `op:'place'` job (one
  release with a deprecation note in the returned digest, then removed
  from the skill doc) — no inline heavy path survives.
- Incremental editors (inline): move/rotate instance, rip bundle, pin a
  topology side, assign plane net, set class rules.
- Gate evaluators (`auto_check`): `netlist_drc_clean`,
  `placement_legal`, `route_complete` — read latest DRC run / route
  statuses (feeds 0042 Slice 9 unchanged in shape).

### Export + order

- **`.kicad_pcb` writer** (export.py): stackup, outline, footprints
  (generated or cached kicad_mod), realized copper, pours. One artifact
  buys: EasyEDA Pro viewing (its KiCad importer), gerbers via kicad-cli,
  and **KiCad DRC as the independent second-opinion oracle** in tests
  (advisory container-image dep; test skips without it, CI job runs it).
- **JLCPCB API client** (`src/precis/pcb/jlc_api.py`): quote → order →
  track over the official API (api.jlcpcb.com); gerber+BOM+CPL bundle
  upload. All calls via `safe_get`/`safe_stream` discipline (fixed host,
  no agent-supplied URLs), creds via `PRECIS_JLC_API_*` env (exported in
  the deploy template — the env-config-vs-CLI-arg gap applies). **Order
  placement always requires explicit human confirmation** — the tool
  prepares and quotes; a human pulls the trigger (spends real money).
- Freerouting DSN path **stays as the escape hatch** — demoted, not
  removed; its deploy residuals in `pcb-0042-implementation.md` demote
  with it.

### Ship order (independently shippable slices, in-file ordering)

1. Schema migration + store mixin + handler read paths (hedges land
   first; everything else keys off board_id/stackup/classes).
2. Footprints: parametric generator + easyeda2kicad wiring.
3. DRC engine + findings rows + gate evaluator. (needs 1, 2)
4. `.kicad_pcb` writer + KiCad-DRC test oracle. (needs 1, 2)
5. Planes: assignment + segmentation + islands. (needs 1, 3)
6. Placer v2. (needs 3, 5)
7. Topological router + autoroute job + congestion/rip-up tools.
   (needs 3, 5, 6)
8. JLCPCB API client. (needs 4)
9. Phase-machine gates hookup — supersedes the "route round-trip"
   wording of 0042 Slice 9; back-edges unchanged. (needs 7)

Each slice ships via the normal gate; live-model/API tests follow the
recorded-fixture pattern (one live smoke gated on creds).

## Explicitly NOT in scope

- Flex fold 3-D clearance DRC, the cad-kind folded-pose generator, and
  any rigid-flex export — only `fold_lines jsonb` (empty) and
  stackup-as-data land now.
- Fluidic/thermal routing behavior — only the `domain` column and the
  rejection message land; no fluidic DRC rules, no fluidic exporters.
- Multi-board routing/mate-link UX — only netlist/board separation
  lands; single board per design remains the handler default.
- Beating Freerouting on completion rate. v1 success = *legible* routing
  with a working guided loop; the DSN escape hatch remains.
- Web UI (ratsnest SVG, board viewer) — stays 0042 Slice 8.
- Datasheet kind (0042 Slice 3), part-selection changes.
- Autonomous order placement (human-confirmed only).
- Removing the DSN/Freerouting code or its deploy role.

## Acceptance criteria

- The ESP32-C3 reference design, on the default 4-layer stackup:
  `op:'place'` then `op:'route'` (worker jobs) reach **100 % routed,
  zero DRC errors** on the dev DB, driven end-to-end through the tool
  surface (no Python-level poking).
- Every routing failure mode produces a `pcb_routes.fail` payload naming
  the blocking gap, the participants, and the clearance arithmetic —
  asserted by tests that *force* failures (undersized board, impossible
  class rules).
- Rip-up loop demonstrated in a test: forced failure → rip named bundle
  → pin alternate topology → re-realize → routed.
- Plane segmentation: a two-rail design yields two islands-free plane
  regions, thermal reliefs on through-hole pads, and a stitching-via
  proposal; a deliberately island-inducing edit is caught as a DRC
  error.
- `.kicad_pcb` export of the routed reference board loads in KiCad with
  **zero KiCad DRC errors** (CI oracle job). (EasyEDA Pro import is a
  post-ship manual follow-up noted in the ship handoff — NOT a gate
  criterion.)
- Placer v2 on the reference design: signal-net crossing count strictly
  below the current `place.py` result; plane-served nets absent from
  the objective (asserted); the digest reports per-term cost and a
  per-region utilization table with its peak identified.
- Footprint generator output for one part per package family matches
  IPC-derived expected pads within tolerance (golden tests); an LCSC
  long-tail part round-trips through the easyeda2kicad path into
  `part_footprints`.
- JLCPCB client: recorded-fixture tests for quote/order/track; one live
  quote smoke (creds-gated) returns a price for the exported gerber
  bundle; the order call path provably cannot fire without the
  human-confirmation token.
- Hydration of a 500-instance / 1500-net synthetic design ≤ 250 ms.
  The autoroute job's gate criterion is **deterministic**: converges on
  the reference board within a fixed negotiation-pass budget (asserted
  as pass count, not time); the ≤ 5 min wall figure is advisory only
  (logged, never gating — sibling-gate load makes wall time flaky).
- Existing v1 surfaces (ratsnest, measures, BOM/CPL/DSN export) green
  and unchanged after the board_id/stackup migration.

## Target + blast radius

- Migration 0138 (new tables + two ALTERs; backfill one board/design).
- `pyproject.toml` — shapely added to core deps ⇒ precis-dev image
  rebuild (or UV_WITH bridge) + serving venvs via normal deploy.
- `src/precis/pcb/` — new: drc.py, planes.py, sketch.py, realize.py,
  jlc_api.py, footprint_gen.py; extended: place.py, export.py,
  footprint.py, catalog.py; eyes.py — drc_lite retired (superseded by
  drc.py behind `view='drc'`), other eyes views unchanged.
- `src/precis/store/_pcb_ops.py` (hydration + new row ops),
  `src/precis/handlers/pcb.py` (tool surface, job enqueue).
- Worker: new job types on the existing substrate
  (`src/precis/workers/`), route/place lanes.
- Skills: `precis-pcb-help` (+ net-class/measures cross-refs) get the
  new ops; a new `precis-route-help` runtime skill.
- Deploy: `PRECIS_JLC_API_*` env in serving templates; kicad-cli in the
  CI/test image (advisory oracle); Freerouting role demoted in docs.
- `docs/backlog/pcb-0042-implementation.md` — residuals section
  rewritten on ship of slice 1 (Freerouting → escape hatch; footprint
  conversion promoted to critical path; Slice 9 wording superseded).

## Open questions / decisions log

- **Decided:** sketch-as-canonical, copper derived (regenerate =
  DELETE+INSERT, chunks discipline).
- **Decided:** stackup as ordered jsonb on pcb_boards (boards are few,
  stackups read as a unit) — not a normalized layer table.
- **Decided:** netlist layer never references geometry; mate links via
  instance meta in v1.
- **Decided:** heavy ops are worker jobs; inline tools stay < ~1 s.
- **Decided:** JLCPCB-first, fab-neutral IR; parts keep MPN+package
  beside the C-number (already in `parts`) so re-sourcing is a lookup.
- Open (slice 7 detail, not a blocker to `ready`): exact sketch anchor
  vocabulary (pins/vias only vs. explicit board-edge anchors) — settle
  in a short design note inside sketch.py's docstring during slice 7,
  with the reference-board tests as the arbiter.
- Open (slice 8): which JLCPCB API auth flow (key pair vs OAuth) —
  settle when creds are provisioned; client abstracts it.

- **Decided (ready-review 2026-08-27, all 4 blockers + 3 advisories
  resolved into the sections above):** shapely = core dep (geom.py stays
  dependency-free; pyproject + image rebuild in blast radius) · reads
  extend the existing `view=` mechanism, `view='drc'` re-backed by
  drc.py, eyes.drc_lite retired · EasyEDA Pro import demoted to a
  post-ship manual follow-up, not a gate criterion · inline `autoplace`
  retired as an alias that enqueues `op:'place'` · slice-3 DRC
  plane/copper rules tested on synthetic fixture rows · autoroute gate
  criterion is a deterministic pass-count budget, wall time advisory.
- **Decided (user, 2026-08-27):** v1 is 4-layer only — a non-4-layer
  stackup is rejected at `op:'place'`/`op:'route'` with a clear message
  (2-layer = routing power like signals, a different problem; later
  item if wanted).
- **Decided (user, 2026-08-27):** length-match / diff-pair class-rule
  fields are **reserved, enforcement out of scope** — the router and
  DRC ignore them in v1; a later slice may enforce.
- **Decided (user, 2026-08-27):** acceptance vehicle = a synthetic
  ESP32-C3 reference design (~30 components: MCU + I2C sensor +
  regulator + decoupling + headers), authored as slice-3 scope, reused
  by every later slice's tests. A real instrument board is the first
  post-slice-7 real-world exercise, not a gate.
- **Decided (algo discussion, user, 2026-08-27):** placer optimizer =
  simulated annealing over a constructive seed (supersedes the earlier
  force-directed wording); congestion objective = peak region
  utilization from a RUDY-style estimator, calibrated post-route by
  realized density; via cost is region-priced congestion, not a scalar;
  plane-served nets connect via dog-bone fanout only (routed gnd trace
  = flagged anomaly); copper retention is a derived fill post-pass with
  an advisory copper-fraction metric, never an optimizer term.
- Open (slice 8, user-side): JLCPCB API access application
  (api.jlcpcb.com) must be filed from the user's account; unknown
  approval lead time — file early, slice 8 is creds-blocked until then.
