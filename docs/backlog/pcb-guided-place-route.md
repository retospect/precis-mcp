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

### Footprint + catalog reality (unblocks everything geometric)

**No parametric generation** (user decision 2026-08-27, supersedes the
earlier IPC-generator plan): footprints are *pulled, never synthesized*.
Rationale: every JLCPCB-assemblable part has an EasyEDA footprint by
construction (JLC assembly places from it), and selection is already
fenced to assemblable in-stock parts — coverage is guaranteed inside
the fence. Policy: **no EasyEDA footprint ⇒ not a selectable part**
(clear message at part selection).

- **PREREQUISITE — wire the catalog ingest at all** (gr264357, found
  2026-08-27): prod `parts` is EMPTY (0 rows) and
  `refresh_parts_from_sqlite` has **zero callers** — there is no
  `precis pcb refresh-parts` verb and no `parts_refresh` worker; only
  the parsing layer in `catalog.py` was ever built. Slice 2 must add
  the entry point (CLI verb + scheduled worker, staging + atomic swap
  per the 0047 design) and correct the messages in
  `handlers/part.py`, `footprint.py`, and `precis-part-select-help`
  that currently name that non-existent command. Everything else in
  this slice — selection ranking, live-stock verification, the
  `datasheet_url` that ADR 0042 slice 3 ingests from — is dead until
  this lands.
  **What feeds it (spike-resolved 2026-08-27).** The existing
  `refresh_parts_from_sqlite` reads the community yaqwsx/jlcparts
  SQLite dump (that project's publish format — SQLite is the dump's
  shape, not our storage choice). Prefer the **official** source:
  `POST /overseas/openapi/component/getComponentInfos` on
  `open.jlcpcb.com` takes a **`lastKey` cursor** — a real incremental
  bulk-pull, the clean path to a local table — but it 403s until the
  app gets the Components scope (see slice 8). Verified fallback with
  **no credentials**: `POST jlcpcb.com/api/overseas-pcb-order/v1/
  shoppingCart/smtGood/selectSmtComponentList` enumerates the whole
  catalog (`total` 7,234,574; empty `keyword` = everything), BUT has a
  hard **100,000-offset cap** (ES `max_result_window`) and `pageSize`
  ≤ 2000 — so a full walk requires sharding into <100k slices
  (keyword-prefix sharding verified: `"C"` → 58,353). Community dump
  drops to last-resort. A bulk-local `parts` table stays required
  either way: offline parametric search/ranking and the
  `part_availability` restock-trend signal (the popularity proxy
  selection ranks on) both need repeated local snapshots, which a
  per-part lookup cannot provide.
- **EasyEDA footprint fetch** (`footprint.py` rework) — **verified
  working, no credentials** (spike 2026-08-27, C42163081):
  `GET easyeda.com/api/products/<C>/components`. Two traps, both
  load-bearing: (a) a bare request gets **403 from CloudFront** — the
  easyeda2kicad header set is required and **`Referer` is the
  load-bearing one**; (b) the footprint is at
  **`result.packageDetail.dataStr.shape`** (`docType: 4`) — plain
  `result.dataStr` is the *schematic symbol* (`docType: 2`), a
  different primitive alphabet that is easy and costly to confuse.
  Primitives are `~`-delimited (`PAD~RECT~x~y~…`, `TRACK~…`), units
  **10 mil**, origin `head.x/head.y`; decoded pads for the test part
  matched an exact 2.54 mm-pitch 2×3 SMD header. Fetch the
  per-C-number EasyEDA component JSON and **parse it ourselves** into
  canonical pads/pin_map/courtyard/centroid in `part_footprints`
  (easyeda2kicad is the reference implementation to crib, NOT a
  dependency — we need only the pad subset, and slice 4's writer emits
  footprints from canonical pads, so no `.kicad_mod` intermediary).
  Raw JSON cached in the row for reparse. Fixed-host fetch via the
  `safe_get` discipline; network-gated like today, no creds needed.
- **JLCPCB Components API client**: live part existence / stock /
  price / basic-vs-extended per C-number, used at selection time
  ("in stock now", not dump-age stock). Creds-gated
  (vault secrets, shared with slice 8); **graceful fallback to
  the existing jlcparts dump (Flow A) + `part_availability` trend**
  when creds are absent — the dump stays the bulk-search substrate,
  the API is the live-verification layer. LCSC.com itself has no
  official API and is not integrated (community endpoints are
  scraper-grade; C-numbers key both surfaces anyway).
- **Selection ranking gains family-consolidation** (the agent-judgment
  half already shipped in `precis-part-select-help`): boost candidates
  sharing a manufacturer *series* with parts already chosen on this
  design, and candidates matching the board's dominant passive package
  (0402 default). Rationale: fewer distinct SKUs = fewer Extended
  loading fees, one set of known characteristics, reorder stability.
  Fit stays a hard gate; turnover (not raw stock) stays the popularity
  estimator.

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
  no agent-supplied URLs). **Creds live in the existing secrets vault**
  (`precis.secrets`, the `/secrets` write-only editor — NOT env vars,
  NOT the deploy template): `JLCPCB_APP_ID`, `JLCPCB_ACCESS_KEY`,
  `JLCPCB_SECRET_KEY`, read via `require_secret`, gated with
  `is_available` so the client degrades cleanly when unset.
  **Auth is SOLVED — server-verified 2026-08-27, do not re-derive:**
  ```
  host = https://open.jlcpcb.com          # NOT api.jlcpcb.com (that
                                          # is the portal SPA; 404s)
  string_to_sign = "{METHOD}\n{path}\n{timestamp}\n{nonce}\n{body}\n"
  signature      = base64(HMAC-SHA256(secret_key, string_to_sign))
  Authorization: JOP appid="…",accesskey="…",timestamp="…",
                     nonce="…",signature="…"
  ```
  Per-request signing; there is **no token-issuance endpoint**.
  Failures are explicit, not opaque: bad signature → `401 The request
  signature verify failed`; unknown app → `401 application not
  exists`; missing header → `400`. Our signature is accepted — the
  only remaining blocker is `403 API insufficient permissions`, i.e.
  **a console permission grant on the app (human action, not
  engineering)**.
  **Politeness/backoff is mandatory** on every JLC + EasyEDA call
  (this is a third-party service that can blacklist us): exponential
  backoff with jitter on 429/5xx, a conservative concurrency cap and
  inter-request delay on bulk walks, honour `Retry-After`, and a
  circuit-breaker that stops the walk rather than hammering. Never
  retry a 401/403 — auth failures must fail fast, never loop.
  **Order
  placement always requires explicit human confirmation** — the tool
  prepares and quotes; a human pulls the trigger (spends real money).
- Freerouting DSN path **stays as the escape hatch** — demoted, not
  removed; its deploy residuals in `pcb-0042-implementation.md` demote
  with it.

### Ship order (independently shippable slices, in-file ordering)

1. Schema migration + store mixin + handler read paths (hedges land
   first; everything else keys off board_id/stackup/classes).
2. Catalog + footprints: EasyEDA footprint pull/parse + JLCPCB
   Components API (live stock) + family-consolidating selection.
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
- Footprint pull: recorded EasyEDA JSON fixtures (one per package
  family) parse into canonical pads/pin_map/courtyard with golden
  assertions; one live creds-free fetch smoke for a real C-number; a
  part with no EasyEDA footprint is refused at selection with the
  documented message (never silently placeholder-padded).
- Selection: given a design already using one resistor series, a new
  resistor search ranks that series' member above an equal-fit
  stranger (family consolidation), and a non-fitting cheaper part is
  absent from candidates entirely (fit is a gate).
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
- Secrets vault rows (`JLCPCB_*`, set via `/secrets`) — no deploy-
  template env change needed; kicad-cli in the
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
- **Decided (user, 2026-08-27):** slice 2 pulls footprints, never
  generates them — no parametric/IPC generator at all. Parts come from
  the JLCPCB Components API (live existence/stock/price, creds-gated,
  jlcparts-dump fallback) and footprints from the EasyEDA per-C-number
  JSON parsed in-house (easyeda2kicad = reference, not dependency).
  No EasyEDA footprint ⇒ not a selectable part. Selection ranking gains
  family/package consolidation; the agent-judgment half of that policy
  shipped immediately in `precis-part-select-help`.
- **Decided (algo discussion, user, 2026-08-27):** placer optimizer =
  simulated annealing over a constructive seed (supersedes the earlier
  force-directed wording); congestion objective = peak region
  utilization from a RUDY-style estimator, calibrated post-route by
  realized density; via cost is region-priced congestion, not a scalar;
  plane-served nets connect via dog-bone fanout only (routed gnd trace
  = flagged anomaly); copper retention is a derived fill post-pass with
  an advisory copper-fraction metric, never an optimizer term.
- **Decided (user, 2026-08-27):** JLCPCB credentials live in the
  existing secrets vault (`/secrets` on melchior, `precis.secrets`)
  alongside the other API creds — names `JLCPCB_APP_ID` /
  `JLCPCB_ACCESS_KEY` / `JLCPCB_SECRET_KEY`, matching the
  `ORCID_CLIENT_SECRET` convention. Supersedes the earlier
  `PRECIS_JLC_API_*`-env plan (no deploy-template change).
- **Spike-resolved 2026-08-27 (live, against the real API).** Slice 2
  needs **no credentials at all** — footprint geometry, stock, price,
  and basic-vs-extended (`componentLibraryType`) are all retrievable
  unauthenticated, so slice 2 is fully decoupled from slice 8's auth
  work (the earlier "creds-gated with dump fallback" framing was
  stricter than reality). Datasheets: records carry **67 fields**
  including `dataManualUrl` (primary PDF) and
  `dataManualOfficialLink`; hosts vary between LCSC-rehosted and the
  manufacturer's own, which **confirms `safe_get` is required** for
  these fetches. Coverage is NOT 100% (the C42163081 test part has no
  datasheet field at all); EasyEDA independently carries a link at
  `packageDetail.dataStr.head.c_para.link`, so the two sources are
  complementary — try both before declaring a part datasheet-less.
- **Resolved 2026-08-27 — creds provisioned.** `JLCPCB_APP_ID`,
  `JLCPCB_ACCESS_KEY`, `JLCPCB_SECRET_KEY` are live in the prod vault
  (names verified against `vault.list()`). Slice 8 and slice 2's
  live-stock half are no longer creds-blocked, and the signing spike
  is DONE (scheme above, server-verified — no Java SDK needed after
  all). **The one remaining blocker is a human console action:** the
  app must have the Components (+ PCB, for slice 8 ordering) API
  permissions enabled, or every signed call returns `403 API
  insufficient permissions`.
