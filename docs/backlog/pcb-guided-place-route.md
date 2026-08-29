---
status: ready
title: LLM-guided topological place+route for the pcb kind (sketch-as-canonical)
prio: high
model: opus
---

# LLM-guided topological place+route for the pcb kind (sketch-as-canonical)

## BUILD STATUS (2026-08-28) — resume pointer

**Shipped to main** (latest `b50bbb87`): slices 1–8 and 10. That is:
schema + board/stackup hedges · catalog ingest,
EasyEDA footprint parser, JLCPCB API client · the L0–L5 IR, objective
vectors and cost function · gerber X2 + Excellon writer + JLC capability
table (numbers **verified against JLC's published specs 2026-08-27**,
not remembered) · footprint escape graph + copper tiling · the joint
optimizer with placement moves · topology/layer/pin-swap moves + the
arcs-and-tangents realizer · the real sweep-line crossings term ·
geometric DRC with its O(n²) reference oracle (slice 8) · the phase-gate
evaluators, `pcb_place`/`pcb_route` job types and `op=` handler dispatch
(slice 10). Slices 8+10 landed together in `0351be2f` on a clean full
gate (15735 passed, 0 failed) after the sibling-congestion window closed.

**2026-08-28 late — the acceptance criterion is MET, and UNSHIPPED.** On
the ESP32-C3 reference vehicle (`tests/fixtures/pcb/esp32c3_reference.json`,
authored this day): **zero DRC errors, 61/61 connections routed, ~0.5s to
realize.** 1063 → 612 → 234 → 0. The full account, including the four
traps that each had to be got right and the design tension deliberately
left open, is in `docs/backlog/pcb-engine-plan.md` §"2026-08-28 late: DRC
to zero" — read that before touching the router. In one line: per-pin pad
geometry, hard placement constraints replacing two graded cost terms the
search was simply paying, and a new occupancy-grid maze router
(`src/precis/pcb/maze.py`) that claims copper before drawing it.

**Zero DRC is trivially achievable by routing nothing** — the router's
first revision did exactly that, 58 of 61 unrouted, and read as a triumph.
Every DRC number in the tests is asserted beside a routed count. Keep them
paired.

A post-ship review of `0351be2f` found three more defects, all fixed and
shipped in `b50bbb87`: (1) `route_complete` was permanently unsatisfiable
on any board with a legitimate <2-member net (test point / NC / mounting
hole) — the route job skipped writing a row, the gate read the missing
row as `unrouted`, and the phase machine wedged forever with no error.
Now such a net gets an explicit `realized` row carrying a *note* saying
why, and the member-count rule lives once in `ir.net_member_counts()`
instead of being duplicated in two components that drifted apart.
(2) Every via DRC rule (`check_annular_ring`, the via halves of
clearance/NPTH) was **unreachable in production** — the realizer emitted
only tracks, so no `ctype='via'` copper was ever persisted, and the rules
passed by never running. **Closed 2026-08-28**: both routers emit real via
geometry (the maze router wherever its search changes layer, sized by the
net's ampacity), `pcb_copper` carries `ctype='via'` rows with a layer
SPAN, and `tests/workers/test_pcb_route.py` exercises the persistence
shape through a via the router genuinely needed rather than a stipulated
one. (3) `pcb_rip_route` / `pcb_copper_list`
were missing the `retired_at IS NULL` filter their siblings all apply.

Still open, filed not fixed: **`gr266041`** — `op=` idempotency coalesces
a resubmit onto an in-flight job that predates an `op='pin_side'` /
`op='plane_net'` edit, because the content hash covers design inputs but
not those session edits. **Resolution decided, not yet built:** this is a
DRY defect, not a caching quirk — the idem key is hand-listed in
`handlers/job.py` while what the run actually depends on is defined in
`pcb_route.py`, and the two drift. Derive the key from the same snapshot
the job reads; then an edit necessarily changes the key and a resubmit is
automatically a new job, with no "predates your edit" band-aid needed.

### Route-state consolidation — DESIGNED, NOT BUILT
Success is **done**: every net that *ought* to be routed is realized AND
nothing is shorted. One definition, one implementation.

- **NOT_STARTED** — no route run yet, *or* the design has no nets →
  evaluator returns `None` ("not yet"). This removes the live incoherence
  where a zero-net design is `False` ("nothing to route is not routed")
  while a board whose only net is a 1-pin test point is `True`.
- **PARTIAL** — some ought-to-be-routed net not realized → `False` + the
  offending nets as issues.
- **DRC_FAIL** — all realized but DRC finds shorts/clearance violations →
  `False` + the findings. Persistent DRC_FAIL is *information* (board too
  small, too few layers), so the issues must be returned, not discarded.
- **DONE** → `True`.

"Ought to be routed" resolves through the single `ir.net_member_counts()`
rule. Implement as one `route_state()` in the pcb domain layer returning
status + issues; `route_complete` and `netlist_drc_clean` both delegate to
it and collapse to the bool the generic machine wants. **Do not widen the
shared `bool | None` Evaluator contract** — seven unrelated evaluators
(`paper_ingested`, `time_past`, `tag_present`, the child-job ones) share it
and none of them need this.

**Slice 9 (JLCPCB ordering) is the only one not built** — blocked on a
human action, not engineering: every signed call returns `403 API
insufficient permissions` until the **Components (and PCB) scope is
granted on the app in the JLCPCB Open API console**. Auth itself is
server-verified and correct; the client is written and fixture-tested.

All three credentials **are** present in the PROD vault (`vault.list()`,
set 2026-08-27 19:42–19:43 UTC). They are NOT in the local dev vault and
NOT env vars, so a local `credentials_available()` returns `False` — that
is a false negative, not a missing key. Probing the scope therefore has
to run where the prod vault resolves, and **an agent cannot do it**: both
a `cluster-ops` spawn and a direct `ssh melchior …` were classifier-
blocked (agent-initiated prod ssh reaching an external API). Hand this to
the human to run instead:

    PRECIS_DATABASE_URL='<the web service's DSN>' \
      /opt/mcps/venv/bin/python -c "
    from precis import secrets
    from precis.store import Store
    from precis.config import load_config
    from precis.pcb import jlc_api
    secrets.bind_store(Store(load_config().database_url))
    print(\"creds_resolve:\", jlc_api.credentials_available())
    c = jlc_api.JlcApiClient()
    if not c.available:
        raise SystemExit(\"STOP: credentials did not resolve — nothing was called\")
    try:
        r = c.component_info(\"C1525\")
        print(\"RESULT: success, got_row:\", bool(r))
    except Exception as e:
        print(\"RESULT:\", type(e).__name__, str(e)[:200])
    "

`success` ⇒ scope granted, slice 9 unblocked. `JlcPermissionError` ⇒
still not granted. `ModuleNotFoundError` ⇒ prod predates `jlc_api`;
re-probe after the next `/go` deploy.

**Two bugs in the previous version of this probe, both live-fixed
2026-08-28 — do not reintroduce them:**
1. `c.available()` — `available` is a **property**, so calling it raised
   `TypeError: 'bool' object is not callable` before any probing happened.
2. Far worse, the probe **could not fail meaningfully**.
   `component_info` returns `None` both when credentials are absent *and*
   when the part is not found (its own docstring says so), so with no
   credentials it returned `None` without ever reaching the network — and
   the probe printed `RESULT: success`. A diagnostic whose success path is
   reachable without doing the thing it diagnoses is worthless; that is
   why the corrected version hard-exits when `available` is false.

**Measured 2026-08-28: the credentials do NOT resolve on melchior** under
a bare `ssh` + `/opt/mcps/venv/bin/python`. That is *not* yet proof the
vault is empty — `secrets.get_secret` reads env → **process-bound store**
(`secrets.bind_store`, never bound in a bare `python -c`) → `~/.secrets/pw/<NAME>`
file. A bare shell has no DSN (`load_config().database_url` is None), so
the DB vault was never consulted at all.

**2026-08-28: the plist is NOT the blocker.** Re-verified that
`~/Library/LaunchAgents/com.precis.web.plist` is unreadable to the ssh user
(`PLIST_NOT_READABLE`) and `/opt/mcps/venv/bin/python` exists (`PY_OK`).
But `scripts/prod-psql` shows the DSN can be built without it: it SSHes to
`caspar` and runs psql against pgbouncer at `100.126.127.107:6432` as
`agent_rw` on `precis_prod`, with the **password supplied by `.pgpass`**.
So the probe should run with a password-free DSN —

    PRECIS_DATABASE_URL="postgresql://agent_rw@100.126.127.107:6432/precis_prod"

— which libpq/psycopg completes from `.pgpass`, so no secret ever appears
in the command or the output. Substitute that for `<the web service's DSN>`
above.

**A human must still run it**: the agent-initiated external API call is
classifier-blocked (re-confirmed 2026-08-28 across both an `ssh melchior`
route and a local one). Routing it through a subagent would be permission
laundering — don't. Hand the user the command with a `!` prefix.

## RAN 2026-08-28 — the credentials blocker was never real

Result: **`creds_resolve: True`**, then
`VendorUnavailable jlcpcb: 5 attempts exhausted; last: HTTP 500`.

**The vault resolves fine, locally, with the pgpass DSN above.** Every prior
session's conclusion — "credentials do not resolve on melchior", "a human
with the web service's `PRECIS_DATABASE_URL` must run it", the whole plist
hunt — was **wrong**, and wrong for a reason worth remembering:

**Bug 3 in this probe.** `Store(...)` takes a `ConnectionPool`, **not a DSN
string**; the DSN constructor is `Store.connect(dsn)`. Passing the string
raised `AttributeError: 'str' object has no attribute 'connection'` deep in
the resolver, which `secrets` caught and reported as:

    secrets: vault reveal unavailable (AttributeError: ...); falling back to
    file/default. Is the migration applied and app.secret_key set?

That message names the vault, the migration and `app.secret_key` — three
plausible, innocent things — while the actual fault was a constructor
argument two lines earlier. **A diagnostic that mis-attributes its own bug to
its subject is worse than no diagnostic**: it sent three sessions after a
permission-denied plist. This is the third bug in one ~15-line probe, and all
three presented as a credentials failure (see the two above).

**Where slice 9 actually stands.** The documented blocker was *403 API
insufficient permissions until the Components scope is granted*. We did
**not** get a 403 — and `jlc_api` never retries 401/403 (a 403 raises
`JlcPermissionError`), so a scope failure could not have been hidden by the
retry loop. Auth and signature are therefore fine. HTTP 500 five times is
JLC's server, not our credentials. **The scope question is still unanswered**
— re-run the probe when JLC is healthy. The enhanced probe at
`/tmp/jlc_probe.py` walks the cause chain and prints the response body,
which distinguishes "JLC is down" from "JLC 500s on a request shape it
dislikes".

### Known-inert / partial — do NOT read as working
- **`SIDE_FLIP` has no cost effect.** A straight-line crossing count at
  L3 cannot read `seg_side`; needs realizer geometry in the loop or
  side-aware gap capacity.
- **`PIN_SWAP` needs admissible-pin data** (datasheet-derived
  equivalence classes + hard exclusions: ESP32 strapping pins, JTAG,
  ADC2-vs-WiFi). Footprint *offsets* are now wired; the equivalence
  sets are not. Degrades to a safe no-op — never invents equivalences.
- **`ROTATE` is cost-neutral** — no registered term reads `inst_rot`.
- **`drc.py` reads only the fab capability table**, not
  `pcb_net_classes` rule overrides; that schema does not exist yet.
  Hook point documented in the module docstring.
- ~~No via geometry is realized~~ — **CLOSED `3cfa3d2e`.** The realizer
  emits `RealizedVia` (span-only, never a scalar layer), `pcb_route`
  persists `ctype='via'` copper, and the annular-ring / via-clearance
  rules now fire. The `view='drc'` caveat has been removed. Two known
  simplifications: via ampacity is a conservative heuristic (plating
  thickness is not stored), and all pads are assumed on stackup layer 0
  (no per-instance mount-side field), which overstates vias for two-sided
  assembly.
- ~~⚠ The via house-defaults are mutually inconsistent~~ — **CLOSED
  `4a9f78d5`** (verified 2026-08-28, this entry was stale).
  `capabilities.py::_derive_via_diameter_mm` now derives the diameter as
  `max(published floor, drill + 2 × annular_ring)` and applies it to both
  the `jlc_min` and `house_default` tiers, so default vias no longer trip
  `check_annular_ring`.
- **⚠ Courtyard overlap is a hard DRC error that nothing optimises
  against — `gr267456`.** `drc.check_courtyard_overlap` errors on ANY
  overlap, and `_render_drc` synthesizes a 1.0 mm courtyard for every
  placed instance, so any two parts within 2.0 mm centre-to-centre fail.
  But `cost.TERMS` has no spatial-exclusion term and `optimize.py` has no
  `overlap|keepout|min_dist` logic at all — `TRANSLATE` is an unconstrained
  gaussian. ~5 expected overlap errors for 30 parts on the synthetic
  board, and no lever to fix them. **This makes the acceptance criterion's
  "zero DRC errors" unreachable by construction**, and promotes the
  seed+spreading work (`pcb-engine-plan.md` S7) to a prerequisite.
- **Layer *roles* are read from the stackup's `role` field**, not yet
  emergent as §Layer ROLE describes.
- **Layer count is hard-guarded to 4** (`handlers/pcb.py:374`): place/route
  refuses any non-default stackup, 2-layer explicitly out of v1 scope. It
  fails *loudly* with a reason and a `next=` hint — the right failure mode,
  just a narrow scope. **Layer count should ultimately be a user call**:
  the machine derives the *floor* (minimum feasible count from escape
  demand and routability), the user picks the actual count at or above it,
  and a pick below the floor is refused **with the reason** — which part's
  escape demand forces the extra layer — never silently promoted. 2-layer
  is dramatically cheaper and is often the right answer.

### Bugs this build produced that were SILENT — all found by tests or
measurement, none by review, none crashed or failed type checking
1. `hardened_penalty` inverted: sub-budget fractions got *cheaper* as
   the schedule hardened (slice 3).
2. SA compared costs across different schedules ⇒ 1-in-3000 acceptance;
   the optimizer wasn't optimizing (slice 6).
3. SA temperature decayed to ~1e-22 before layer moves became eligible
   ⇒ 2174 proposals, 0 accepted; fixed by stage-boundary reheat (7).
4. The crossings estimator was **provably always zero** on any real
   board (forest ⇒ Euler bound never fires).
5. **Every via was invisible to clearance DRC** — vias carry
   `layers`/`span`, the indexed engine read `layer`, so all vias were
   dropped from every layer's candidate set. A shorted board would have
   passed clean. Found by the O(n²) oracle at gap 0.0 (slice 8).
Keep the oracle and the delta-correctness property tests permanently;
they are what makes this class of bug findable.

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

A **4-layer default stackup simplifies the problem**: nets served by a
plane become "drop a via to the plane", removing ~⅓–½ of the ratsnest
from both the placement objective and the router.

**Layer identity is an INTEGER INDEX, not a name** (user decision
2026-08-27). Internally a layer is its position in the stackup array;
`"F.Cu"`/`"In1.Cu"` are KiCad's vocabulary and gerber has its own
file-per-layer convention, so **names are an export concern only** and
must never appear in optimizer state. The payoff is not just tidiness:
with integer layers a via's span is a contiguous **bitmask**, "does
this via block layer k" is a bit test, and pad layer membership is the
same, so the inner loop does arithmetic instead of string hashing. The
already-shipped `stackup jsonb` is an *ordered* array, so index is
already its identity — this is a discipline statement, not a schema
change.

**Layer ROLE is a decision variable, not a constant** (user decision
2026-08-27). Do NOT hardcode SIG/GND/PWR/SIG. Which nets become planes,
and on which layers, is exactly the same *kind* of choice as which
layer a segment lands on — a plane-assigned net is just a net that
occupies a large connected region instead of a thin path, so it stops
consuming gap capacity (it *is* the gap) and removes its pins from
routing. That makes role assignment another move class, and lets better
architectures emerge by measurement rather than convention (three
routing layers + one GND for a dense BGA; a partial pour instead of a
whole wasted PWR layer for a single-rail board). Layer *count* and
*physical* stackup stay constrained by the fab menu (slice 4's
capability data); **roles are entirely ours**.
Two guards keep emergent roles from producing electrically silly boards
— both cost terms, neither a hardcoded stackup:
- **Reference-plane adjacency.** Not only an RF concern: any fast edge
  (USB, a 100 MHz SPI clock, a 1 ns CMOS edge) needs a return path
  beneath it, and a signal layer with no adjacent contiguous reference
  gets a long return loop — EMI and ringing that are miserable to
  debug. Term: each signal layer wants an adjacent layer carrying
  substantial contiguous copper on a stable net. Low weight while we
  are slow-digital-only; the same term takes a high weight when RF
  arrives, with no structural change.
- **Copper balance.** Fabs want roughly balanced copper per layer or
  the panel warps. This stays a **check, not a cost term** — the
  tiling pass coppers every layer to near-full by construction, so
  balance is largely automatic and the residual comes from antipad and
  clearance density rather than from trace width.

Consequence for the never-route-ground rule: it was predicated on
*knowing* which nets are plane-served. The rule is unchanged but its
antecedent is now decided by the optimizer — **if** a net is
plane-assigned on some layer, its pins fan out (dog-bone) rather than
route.

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

### The IR: one progressively-enriched structure (`ir.py`)

User decision 2026-08-27, and the organizing principle for every engine
below. **One structure, six enrichment levels.** Each level *decorates*
the previous — drop everything above level k and you still hold a valid
level-k object. Optimization runs as deep in pure-graph space as it can
and only descends when it must.

| L | Adds | Decidable at this level | Cost to evaluate |
|---|---|---|---|
| **L0** | pins + nets (hypergraph) | connection trees, **pin/gate swap classes**, clustering, bundles | µs |
| **L1** | integer layer per segment; vias as transitions | biplanarity partition, via count/spans, **which nets become planes**, plane connectivity under via deletion | µs |
| **L2** | explicit combinatorial embedding (rotation system + side choices) | crossings, relative order (`A north of Z`) | µs–ms |
| **L3** | component x/y/rotation; pin positions from footprints | courtyard overlap, board fit | ms |
| **L4** | metric annotations: gap capacities, region density, class width minima | routability-for-real, congestion | ms |
| **L5** | realized copper: arcs, tangents, widths, pours, antipads | geometric DRC | seconds |

**The invalidation cascade is the point, not the tidiness.** A move at
level k dirties only levels > k, and usually only *locally*:

- move/rotate a component → L3 dirty, L4 dirty **near that component
  only**, L5 dirty; **L2 and L1 stay clean**. That invariance IS the
  rubber-band property, and it is what makes the joint optimizer's
  local-delta requirement satisfiable.
- flip a side choice → L2 local, L4 local. L0/L1 clean.
- reassign a layer / promote a net to plane → L1, and L4 for the
  affected gaps. L3 untouched.

**Store the embedding EXPLICITLY; never derive it from coordinates.**
This is the crux. If L2 is recomputed from L3 positions, every
component move silently re-derives the topology and the invariance
above is lost — we would be back to maze-router behaviour with extra
steps. Storing chosen sides/orders means a move *preserves* topology by
construction and we only check it is still realizable. L3 coordinates
**propose and validate** the embedding; they do not define it. (So this
is a lattice with one back-edge, not a strict ladder — be honest about
that rather than pretending L2 is coordinate-free in practice.)

**Implementation shape: arrays, not objects.** Parallel numpy arrays
keyed by segment id; layer as `int8`; via spans as bitmasks; adjacency
in CSR; dirty flags as boolean masks. That is what buys vectorized
batch delta evaluation and gets past Python's per-op overhead — an
object graph of `Segment` instances will not hit the move rates the
optimizer needs.

**Storage maps onto the levels directly**: L0–L3 are the canonical
sketch in Postgres (`pcb_routes.topology`, instances); L4 is derived
cache; L5 is `pcb_copper`, derived and regenerable — the discipline
already shipped in slice 1.

**A win that only exists in graph space: pin and gate swaps.** Many
pins are functionally interchangeable — resistor ends, gates within a
logic IC, and above all **MCU GPIO assignments**. Reassigning ESP32
GPIOs is a pure L0 relabeling that can collapse a large fraction of
crossings at zero physical cost. Human designers do this by hand and
hate it; here it is just another cheap move class, available precisely
*because* we optimize combinatorially before committing to geometry.
Requires the netlist to carry pin-equivalence classes (a `part`/
footprint annotation) — worth capturing when the catalog lands.

### Connections carry OBJECTIVE VECTORS (`objectives.py`)

User decision 2026-08-27. Every connection carries a vector of physical
objectives — **low impedance · low resistance · low capacitance · small
loop area · low coupling · matched length** — and the optimizer
minimizes them jointly. This *replaces* a bespoke decap/affinity-edge
mechanism outright; do not reintroduce one.

**Why this beats special-casing.** "Decap near its IC" stops being a
rule and becomes a consequence of `C<tmp> terminal A: low impedance to
PWR; terminal B: low impedance to GND`. No group table, no decap
pattern-matcher. The same machinery covers the switcher hot loop, the
crystal, sense resistors and terminations — they differ only in which
objectives their connections carry.

**It absorbs the width-policy enum too.** fixed/minimum/free is not a
separate classification: **low capacitance ⇒ narrow, low resistance ⇒
wide**, and the tiling pass reads the objective directly. One fewer
concept.

**Refinement that is physics, not pedantry — impedance is a LOOP
property.** "Low impedance to PWR" is satisfiable by a via anywhere,
since a plane is low-impedance everywhere — but the quantity that
matters is the loop `IC pin → plane → cap → plane → IC pin`, whose
spreading inductance grows with separation. So an impedance objective
**implicitly names its return**; nets are already domain-typed and
classed, so a PWR connection's return is its paired GND and the
objective becomes loop-scoped without per-instance statement.
(Consequence, and a cargo-cult correction: with tight plane-pair
spacing that inductance grows ~logarithmically, not linearly, so decaps
may legitimately sit further out than the 2 mm folklore. Minimizing the
real quantity lets the optimizer discover that instead of obeying a
rule of thumb.)

**Why this mostly dissolves weight-tuning.** Objectives in real units
(nH, mΩ, mm²) are commensurable in a principled way, so the usual
hand-tuned-weight problem largely evaporates. What does NOT evaporate:
converting between them is a judgment (how many nH is one crossing
worth?). Keep that as a **handful of meaningful dials** — "EMI-
critical", "cost-critical" — never dozens of opaque weights, and
expose the trade as a **Pareto front** (one placement minimizing loop
inductance, one area, one layer count) rather than a single scalar
going down. A front is far more legible to a model-in-the-loop than a
number.

**Every cost term must carry a one-line justification** naming the
physics or manufacturing constraint it encodes. A term that cannot
produce one is suspect — this is how a defunct convention
("penalize wirelength", "prefer 45°", "split planes for isolation")
gets caught at the point it would enter, instead of living forever as
an innocuous config constant. "Fail legibly", applied to our own
assumptions.

### The cost function (`cost.py`) — one function, refined estimators

Everything else in this spec depends on this being right, so it is
written out in full rather than left implicit.

**It does NOT change across levels.** One cost function, evaluated by
progressively finer *estimators*. If the objective itself varied per
level, an improvement at L1 would not survive to L4 and the optimizer
would thrash — commit coarse, descend, discover the real cost moved the
other way. Three things that are easy to conflate and must stay
separate:
- **the cost function** — constant across levels AND across the schedule
- **estimator fidelity** — increases as you descend levels
- **constraint hardness** — increases across the schedule

**Admissibility rule: every coarse estimate must be OPTIMISTIC.** It may
understate cost and overstate feasibility, never the reverse (A*
admissibility). The guarantee bought: descending only ever brings *bad*
news, so a state that looks bad at L1 really is bad and can be pruned
without discarding a good solution. The natural estimators have this
for free — Euclidean pin distance ≤ routed length; same-layer crossing
count ≤ vias required; courtyard sum ≤ area.
**Testable as a property**: generate random states, evaluate at L1 and
L4, assert L1 bounds L4. This is the check that stops the estimator
hierarchy silently rotting; nothing else would catch it.

**Trap — undefined ≠ zero.** Gap capacity is undefined at L1 (no gaps
without pad positions). Evaluating an undefined term as *zero* tells
the optimizer congestion is free, and it will produce states that look
excellent at L1 and are unroutable at L4. Undefined terms evaluate as
an optimistic **bound**, never as nothing.

**Two families, normalized differently, AND AGGREGATED DIFFERENTLY:**
- **Margin terms** → normalized by their own budget, giving a
  dimensionless *fraction of allowance consumed* (this clearance is
  0.6 of fab headroom; this coupling is 0.3 of the victim's noise
  budget). Clearance, inductance, coupling, thermal rise, gap capacity,
  **and same-layer crossings**.
- **Money terms** → normalized to currency from live JLC pricing.
  Board area, layer count, via count, Extended-part fees.

**`crossings` was MISSING from this list until 2026-08-28 — a real spec
bug, found on contact in slice 7.** The layered-ratsnest section calls a
same-layer crossing "exactly the thing that must be resolved", but the
term list never included it, so `LAYER_ASSIGN` and `SIDE_FLIP` shipped
**cost-neutral**: the optimizer could not reduce crossings because
nothing measured them, and the entire rationale for layering the
ratsnest was unimplemented. Reads `ir.same_layer_crossing_bound`.
Margin family, but its budget is **zero** — a same-layer crossing is a
violation, not a quantity to trade — so the fraction is
`crossings / tolerance` with tolerance shrinking over the hardening
schedule: soft early so the optimizer can pass through crossing states,
hard at the end. Lesson worth keeping: an architecture section and a
term list can disagree silently, and the code will faithfully implement
the term list.

**FOLLOW-UP FINDING (2026-08-28) — the first crossings estimator was
admissible but PROVABLY ALWAYS ZERO.** `ir.same_layer_crossing_bound`
is the Euler bound `E − (3V−6)`. `from_graph` star-decomposes nets, and
one physical pin belongs to exactly one net, so a layer's segment graph
is always a **vertex-disjoint forest** — and a forest satisfies
`E ≤ V−1 ≤ 3V−6` unconditionally. The bound is therefore zero by
construction on any board built the normal way, not merely small.
Root cause: it answers *"is this graph forced non-planar?"* (for a
forest, never) instead of *"do these segments cross in the current
layout?"* (constantly). A forest is planar in the abstract and can
still be drawn with many crossings.
**Fix: the cost term is a sweep-line count of actual segment
intersections at L3** (`O(n log n + k)`), which measures the thing we
mean. The Euler bound stays, demoted to a pure *feasibility* predicate
(is this layer forced non-planar), which is what it was always
actually computing.

**This revises the admissibility rule to be TWO-SIDED.** The geometric
crossing count is an **upper** bound on routed crossings (the realizer
can sometimes route around one), whereas the rule as first written
demanded coarse estimates be *lower* bounds. Both directions are sound;
the guarantee simply mirrors:
- **lower bound** ⇒ "looks bad ⇒ really is bad" (safe to prune).
- **upper bound** ⇒ "looks good ⇒ really is good" — straight-line
  crossings of zero guarantees routed crossings of zero, which is
  exactly the guarantee needed to drive a term to zero.
What matters is that each estimator's **direction is declared and
tested**, not that every estimator points the same way. The property
test must assert the declared direction per term rather than a single
global inequality.

**Still open after this fix: `SIDE_FLIP` remains cost-neutral.** A
straight-line count at L3 does not read `seg_side`, so side choices
have no cost effect until either the realizer's geometry enters the
loop or gap-capacity accounting becomes side-aware. Recorded as a known
inert move class rather than left to be misread as working.

**Aggregation: money ADDS, risk does NOT average away.** Sum the money
family. Take **exact max** over the margin family —
one net at 99 % of its noise budget is a problem even if 500 others sit
at 5 %, and a sum drowns exactly the signal that matters. (Same
instinct as "penalize peak region utilization, not variance" in the
congestion estimator — generalized here, having originally been applied
only there.)
**It must be EXACT max, not a soft-max / p-norm** (found on contact,
slice 6 — this spec previously offered the p-norm as an equal option,
which was wrong). A p-norm shifts with *every* cached value's relative
weight, not just the peak, so it is **not locally decomposable** and
violates the optimizer's locality hard-constraint: no bounded per-move
delta exists for it. `OptimizeConfig` rejects a non-None `p_norm` at
construction rather than letting it silently destroy delta correctness.
A plain max changes only when the peak changes, which a local delta can
track.

**Convexity IS the hardening schedule** — they are one mechanism, not
two. Margin penalties are superlinear in budget fraction (~quadratic
early, so the optimizer can explore through near-violations), sharpening
toward a barrier at the end so violations must actually reach zero.
Linear summing under-penalizes near-violations: a design at 95 % of
every budget is far riskier than one at 50 % and 40 %, because the
first has no room for manufacturing variation.

**Coefficients: ONE irreducible dial**, not a table of weights — the
exchange rate between risk and money. Everything else derives from it
plus a **criticality class** per constraint *type*, drawn from a small
enumeration (catastrophic / functional / marginal / cosmetic) reflecting
*consequence of violation*, assigned once and justified by physics.
Note only relative weights matter (n terms ⇒ n−1 degrees of freedom),
and the dial need not be guessed — sweep it for the Pareto front, which
turns the one judgment call into a visible trade curve.

**Wirelength is deliberately ABSENT** — the primary objective of every
other tool. It is **subsumed, not omitted**: length enters through
*resistance* where current matters, *inductance* where di/dt matters,
*delay* where timing matters, and is correctly ignored where a net
cares about none of them. This removes the commonest way a placer
produces tidy-looking, electrically mediocre boards.

**Per-connection annotations** (LLM-derived at part ingestion from
datasheet timing tables and input specs, with a fallback library keyed
by function — a crystal node is high-Z by construction, a switcher SW
node a violent aggressor, an ADC input a sensitive victim):
**impedance · edge rate · signal level**. From those the coupling term
derives rather than being asserted:
`coupling(a→v) = aggressor_strength(a) × victim_susceptibility(v) ×
k(geometry)`, where high-Z ⇒ capacitive (E-field) victim, low-Z ⇒
inductive (H-field) victim, high dV/dt ⇒ capacitive aggressor, high
dI/dt ⇒ magnetic aggressor. **Cheap despite being pairwise**: coupling
decays fast with distance (a spatial query we already run for gaps),
and most nets are neither strong aggressors nor sensitive victims, so
the significant pair list is dozens, not thousands. **Order-of-magnitude
accuracy suffices** — the geometry term varies over a far wider range
than the annotation error, so false precision here would only invite
over-fitting.

**Via cost is net-dependent and runs BOTH directions** — a good sanity
check that the objective vector is doing real work rather than
relabelling special cases: high-Z ⇒ avoid vias (≈0.5–1 pF against a
huge R, plus barrel leakage, plus guard-continuity break); controlled
impedance ⇒ avoid vias (discontinuity + stub resonance); **high current
⇒ deliberately MANY vias in parallel** (a 0.3 mm via carries ~1–2 A, so
a 10 A rail needs an array). Extreme cases also take a hard
`max_vias: 0` cap that no weighting may trade away — which is also an
L1 layer-assignment constraint (the net is single-layer), to be
respected rather than discovered.

**Every term carries a one-line justification** naming the physics or
manufacturing constraint it encodes. A term that cannot produce one is
suspect. This is how a defunct convention ("penalize wirelength",
"prefer 45°", "split planes for isolation") gets caught at the point it
would enter, instead of living forever as an innocuous config constant.

**Calibration — structure is sound, NUMBERS ARE NOT VALIDATED.** Say so
rather than dressing a guess as a derivation. Division of labour: the
**LLM labels and attributes** (reads a DFM report / bench observation,
attributes a failure to a term, sanity-checks a fitted coefficient for
physical plausibility); a **numerical routine fits** (not token
generation); the **LLM reviews** the fit. Same candidate→verify shape as
part selection. The binding constraint is *data*, not capability — each
electrical data point costs a fabrication cycle.
**Entry point that needs no fabrication: rank, don't calibrate.** Take
published reference designs (dev kits, app-note layouts) as positives
and generate negatives by deliberate perturbation — scramble placement,
split a plane, walk the decaps away, force vias onto a high-Z net — and
require the cost function to order them correctly. Free labels,
unlimited synthetic data, runs in CI as a regression suite. Cheap
sources in increasing order: reference-design ranking (free, now) →
JLC's DFM report per order (cheap, covers manufacturing terms) → bench
measurement (expensive, the only source for EMI/SI).

**Open items — recorded as open, NOT quietly resolved:**
- The risk↔money exchange rate is a guess with a trade curve behind it.
  The ranking harness catches only *gross* errors; it does not
  discriminate among near-optimal designs, which is where tuning
  actually matters.
- **Terms are not independent** and the sum pretends they are: widening
  a trace lowers resistance, raises capacitance and consumes gap
  capacity at once. Survivable (all three derive from the same
  geometry, so the coupling is implicit and consistent) but it means a
  term cannot be tuned in isolation — which is exactly what people try.
- **No board-level thermal term** (component-to-component heat,
  spreading, hotspots). Fine for logic boards, wrong the moment a
  regulator dissipates a couple of watts. Known-absent by decision.
- **Nothing for assembly/test** — AOI orientation, test-point access,
  panelization. Known-absent by decision.

### Refdes and pin maps are LATE LABELS with a freeze point

User decision 2026-08-27. `C1`/`R7` are assigned at schematic capture
and then treated as identity for the board's life, purely because of
the order someone drew them. They are **labels**. Internal identity is
a stable opaque id; **refdes is assigned at export** in a spatially or
functionally sensible order — which incidentally yields far nicer
boards to assemble and debug than arbitrary capture order.

**Hazard, same shape as the firmware pin-map desync:** refdes is free
until first release, then **frozen**. Once physical boards exist with a
BOM/CPL/assembly drawing naming C5, renumbering silently desyncs
paperwork from hardware. Both refdes and pin assignment are *generated
artifacts* with an explicit freeze recorded as board state — never a
convention someone remembers.

### Engines (all in `src/precis/pcb/`, pure-Python, unit-testable)

Dependency: **`shapely` becomes a core runtime dep** (pyproject `[project]`
deps, beside numpy which is already core) — DRC/plane reads run inline in
serve, so extras-gating would repeat the asa-venv silent-fail trap. Its use
is confined to drc.py / planes.py / realize.py; **`pcb/geom.py` keeps its
"no dependencies" convention untouched**. New-core-dep fallout applies:
precis-dev image rebuild (or the UV_WITH bridge until then) + serving
venvs pick it up via the normal deploy.

- **DRC splits in two, at the IR level boundary** (re-cut 2026-08-27
  after the layered-ratsnest/IR decisions — the single-engine `drc.py`
  this bullet used to describe no longer matches the architecture):
  - **Graph feasibility** (`ir.py`, L0–L4, **no geometry, no
    shapely**): unconnected items, same-layer crossings, per-layer
    planarity, per-gap capacity, plane connectivity under via
    deletion, escape-capacity feasibility. These are *constraints
    inside the optimizer's loop*, not a post-hoc pass — but they also
    back `view='drc'` on their own, which means the useful early
    answer ("this netlist cannot route on 4 layers, here is the gap
    that binds") arrives **before any geometry exists**.
  - **Geometric DRC** (`drc.py`, L5, on realized copper): class-rule
    clearance, width, annular ring, courtyard overlap, board-edge.
    The *final* check, not the main event. Verified against the
    **naive O(n²) reference implementation** (exact pairwise distance
    on small fixtures) — the acceleration structure is where the bugs
    live, and a reference kills them instantly.
  Both emit `pcb_drc_findings` rows + a TOON digest and share
  `view='drc'`; `eyes.drc_lite` is superseded, eyes.py keeps
  ratsnest/crossings/proximity/measures.
  **Shapely's justification changes** (it is still a core dep, for a
  different reason): NOT for clearance queries — our geometry alphabet
  is small (circles, rounded rects, arcs and segments with width) and
  those distances are closed-form. It is needed for the **tiling
  pass**: polygon booleans, antipad subtraction, sliver detection,
  weighted-Voronoi expansion. If tiling were ever cut, the shapely
  core-dep decision should be revisited rather than inherited.
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
- **Joint place+route optimizer** (`optimize.py`, absorbing the
  `place.py` extension): ONE annealer over a shared
  **(placement, sketch)** state — placement and topology are not
  separate stages (user decision 2026-08-27; see the decisions log for
  the full rationale). The staging in classic tools is an artifact of
  maze routing, where a route *is* geometry and any component move
  invalidates it. In a rubber-band sketch a route is a set of
  combinatorial side-choices that stays *valid* under small placement
  perturbations — the bands just stretch — so co-optimization is the
  payoff for choosing the topological representation, not a stretch of
  it.
  **Hard design constraint — locality.** Every cost term MUST decompose
  into local contributions with an efficient delta: moving one
  component perturbs only the connections incident to it and the gaps
  near it (~10–30 on a real board). A term needing a board-wide
  re-evaluation per move is disqualified however cheap it looks — rule
  simplicity does not save an O(board) delta.
  **MEASURED throughput — ~880 moves/s** (2026-08-28, synthetic
  30-component / 49-segment board, all move classes). Trajectory across
  the build, all on the same host/board: ~1200 placement-only (slice 6)
  → ~1050 with topology moves (slice 7) → ~1348 with the Euler-bound
  crossings term → **~880 with the real sweep-line crossings term**.
  The last step is a genuine ~35 % regression and the honest price of
  measuring crossings instead of not measuring them; it stays bounded
  because the per-move delta is O(layer size), never O(board).
  This **replaces the ~10⁴ moves/s figure originally asserted here**,
  which was an unmeasured estimate, an order of magnitude optimistic,
  and had been propagating into design decisions unchallenged.
  Consequence: 10⁵ moves ≈ 2 min (fine), 10⁶ ≈ 19 min, 10⁷ ≈ hours — so
  "minutes per board" holds only at the LOW end of the SA appetite
  range. Still acceptable
  (place/route are enqueued worker jobs by construction, never inline),
  but if slice 7's topology moves enlarge the search space enough to
  need 10⁶+, vectorizing the delta becomes load-bearing rather than
  optional. **Re-measure after slice 7 rather than assuming.**
  **One honest exception to the locality rule: `board_area`.** A
  bounding box is irreducibly a whole-board aggregate and cannot
  decompose locally. It is recomputed per move at O(n_instances), not
  O(board geometry), and documented as the deliberate exception in
  `optimize.py` — recorded rather than papered over, since a rule with
  an undocumented violation is worse than one with a documented one.
  **Move mix is a schedule, not an architecture.** Start
  placement-dominant (topology is meaningless before parts are roughly
  located), end topology-dominant (once placement is near-frozen the
  remaining wins are side-choices and layer assignment). Placement and
  topology moves have different cost scales and acceptance rates, so
  the mix adapts over the schedule.
  Constructive seed (connectivity clustering + cluster drop) then SA as
  the refiner. Placement moves: translate, rotate 90°, swap-pair;
  `fixed='xy'|'rot'|'both'` = restricted move sets, locked parts still
  contribute cost. Topology moves: flip a side choice, reorder through
  a gap, change layer, rip-and-reseed a bundle. Cost = weighted legible
  terms:
  signal-net crossings (**plane-served nets excluded**) +
  **peak region utilization** from a RUDY-style grid estimator (net
  demand smeared over bounding boxes vs. per-region track capacity;
  penalize the peak, NOT variance — evenness emerges, clusters stay
  clustered) + region-priced **via demand** (gnd/power pins count as
  vias; a via blocks all layers, so its cost is the local congestion,
  not a scalar) + courtyard-overlap + measures as soft terms. Digest:
  per-term, per-region table + per-component move list; the LLM's
  lever is re-annealing from current state with adjusted weights/locks.
  Post-realize, measured per-region density replaces the estimate
  (predicted-vs-realized per region is the estimator's calibration
  signal).
  **Legibility is a requirement, not a hope.** A fused optimizer's
  failure mode is one number going down with no story — which would
  break the whole "LLM intervenes between rounds" premise. The engine
  MUST keep per-term, per-region cost decomposition, so the digest can
  still say "peak congestion in region C3, driven by these six nets,
  and placement moves there are blocked by two locked parts." This is
  the thing most likely to be quietly lost in the fusion.
- **The canonical intermediate is a LAYERED ratsnest** (user decision
  2026-08-27 — the representation, not just the optimizer, changed).
  A connection is a path of segments; **every segment carries a layer**;
  every layer transition is a via with position constraints. Layer
  assignment lives INSIDE the ratsnest, not in a pass after it.
  *Why this beats the layer-free version:* a crossing in a layer-free
  ratsnest is not a violation — it is only a violation if both segments
  land on the same layer. Minimizing layer-free crossings optimizes a
  *proxy*. With layers, a crossing is exactly the thing that must be
  resolved, and resolution has exactly three forms — move a component,
  reorder through a gap, or spend a via — so via cost falls out of the
  structure instead of being a hand-tuned term.
  *The graph formulation:* "partition edges into k subgraphs, each
  planar" is graph **thickness**; planarity testing is linear-time and
  crossing counting is a sweep line, so evaluation on ~1500 connections
  is milliseconds — the cost is in the search, not the check. Lineage:
  Dai/Kong/Sato on rubber-band-sketch routability.
  *Big simplification from our own stackup:* with the default 4-layer
  SIG/GND/PWR/SIG, only F.Cu and B.Cu route — signal layer assignment
  is **binary**. We solve biplanarity, not general k-layer thickness.
  **Three places the pure-graph view must take metric information
  back** — this is the load-bearing part, a purely topological
  formulation will cheerfully emit physically impossible boards:
  1. *Crossings depend on placement*, so this never fully decouples.
     What is needed is the **cyclic order of pins around each
     component** plus rough relative positions — enough geometry to
     determine the embedding, not enough to draw it. That is what keeps
     the state small.
  2. *Planarity is necessary but NOT sufficient — gaps have capacity.*
     A perfectly planar assignment can still demand 8 strands through
     a 0.5 mm gap. So **per-gap capacity accounting moves OUT of the
     realizer and INTO the optimization loop** as a hard constraint
     (strands passing vs. strands that fit, per adjacent-obstacle
     pair). Cheap to maintain, keeps the representation nearly pure.
  3. *Vias couple all layers.* A through via blocks every layer, so in
     the graph it is a vertex present in **all** layer subgraphs at
     once. That coupling is why this does not decompose into k
     independent planarity problems, and it is the part that stays
     genuinely hard. (Blind/buried vias would decouple it; JLC prices
     accordingly.)
  **Plane integrity becomes a constraint, not a post-hoc finding.**
  Every via punches an antipad through a plane; enough antipads in one
  region disconnect it. That is a graph-connectivity property with vias
  as vertex deletions — so the island detector moves *into* the
  optimizer's constraint set rather than reporting damage afterwards.
  **Consequence for the congestion estimator:** RUDY was a statistical
  proxy for gap pressure. Once gaps carry exact capacities we can
  *count* instead of estimating, so RUDY is retained ONLY for the early
  placement-dominant phase when no sketch exists yet; the moment a
  layered sketch exists, exact gap capacity supersedes it.
  **Scheduling:** layer assignment must not start at move zero — while
  placement is making large moves the assignment churns uselessly. It
  enters mid-schedule: placement-dominant → layer assignment enters →
  topology polish.
- **Sketch + realize** (`sketch.py` + `realize.py`): net → two-pin
  connection tree; the layered sketch state (side choices + layer
  assignment) is what the joint optimizer mutates. **The realizer stays OUT of the inner loop** —
  sketch → copper is deterministic and comparatively expensive, so it
  runs at checkpoints only, preserving sketch-as-canonical /
  copper-as-derived. Deterministic realizer with per-gap capacity
  accounting; congestion digest per board region; rip-up primitives
  (rip one bundle/net, pin a topology choice, re-realize
  incrementally). Escalating-cost reroute (PathFinder-style
  negotiation) across checkpoints; the LLM intervenes between rounds,
  not per segment.
  **Geometry is arcs and tangent lines, NOT beziers** (user question
  2026-08-27, resolved to the exact form): the shortest path around
  obstacles with clearance is exactly straight tangents joined by
  circular arcs, computable in closed form — a bezier would be an
  *approximation* of something we can solve outright. Gerber supports
  arcs natively (G02/G03) and has no bezier primitive, so beziers
  would be flattened to polylines on export anyway. Arcs are therefore
  simultaneously more correct, cheaper, and more manufacturable, while
  still buying the real wins of curved traces (no acid traps at acute
  angles, better copper utilization, smoother impedance).
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

- **Gerber X2 + Excellon writer** (export.py) — **replaces the
  `.kicad_pcb` writer as the critical path** (user decision
  2026-08-27). Rationale: routing through `.kicad_pcb` to reach gerbers
  is backwards — it writes a complex, version-brittle s-expression
  board format so an external binary can convert it into a *simpler*
  one. Gerber X2 is flat (aperture definitions + draws) and Excellon is
  trivial; writing them straight off canonical copper is less code than
  the board writer, removes the `kicad-cli` image dependency, and drops
  a class of "KiCad N changed the board format" breakage. `export.py`
  already hand-writes BOM/CPL/DSN — this is the same kind of work.
- **JLC capability rules as versioned DATA, not code**: a rule table
  holding JLC's published minimum *and* our house default at a
  deliberate margin above it. Two tiers so the margin is legible: the
  DRC digest can say "JLC min 3.5 mil, house default 6, this trace
  spends 2.5 mil of headroom" rather than hiding a constant. Data (not
  code) because aluminum and non-4-layer processes have *different*
  capability rows — the same reason the stackup is data.
- **DRC oracles without KiCad.** Conservative margin defends against
  *quantitative* error (units bugs, a few mils of clearance) but NOT
  against *categorical* error (a net silently shorted, an unconnected
  pin called clean, a pad on the wrong layer) — margin is irrelevant
  to those, so slice 3's engine still has to be correct. Two cheaper
  oracles cover it better than KiCad did:
  (a) a **naive O(n²) reference implementation** — exact pairwise
  clearance on small fixtures, asserted equal to the STRtree-
  accelerated engine. Spatial-index bugs (a query that misses a
  neighbour) die instantly against this; ~30 lines, no dependency.
  (b) **JLCPCB's own DFM report** on upload at slice 8 — ground truth
  from the authority that matters, not a proxy for it.
  Residual gap, stated honestly: nothing independently checks our
  *interpretation* of a rule's definition (clearance to centreline vs.
  edge). That is exactly the error class the house margin absorbs, and
  JLC's DFM catches the remainder before copper is etched.
- **`.kicad_pcb` writer — demoted to a convenience**, for human viewing
  in KiCad / EasyEDA Pro. Worth having, not worth blocking on; nothing
  correctness-critical rides on it, so it may be lower fidelity and
  ships whenever convenient.
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
**Re-cut 2026-08-27.** The old order put a geometry-first DRC engine at
slice 3 and the IR nowhere — an artifact of the pre-fusion design. The
IR is now the foundation everything keys off, and geometric DRC needs
realized copper (L5), so it moves *after* the realizer. Net effect: one
foundation slice earlier, one geometry slice later, same count.

3. **The IR** (`ir.py` + `objectives.py`) — L0–L4 structure, enrichment
   levels, dirty-flag cascade, array layout, objective vectors; plus
   the **graph feasibility checks**, which back `view='drc'` with
   useful findings before any geometry exists. Foundation slice: like
   slice 1, little user-visible surface, everything downstream depends
   on it. (needs 1, 2)
4. Gerber X2 + Excellon writer + JLC capability rule table. Independent
   of 3 — the widest point in the graph, ship in parallel. (needs 1, 2)
5. Escape-routing precompute per footprint + plane/tiling primitives
   (weighted-Voronoi expansion, sliver cull, tile connectivity).
   (needs 2, 3)
6. Joint optimizer, **placement moves only** — the walking skeleton.
   Same engine as 7, restricted move set. (needs 3, 5)
7. Joint optimizer, **topology + layer + pin-swap moves enabled** +
   realizer (arcs/tangents) + autoroute job + congestion/rip-up tools.
   Not a second engine: 6 and 7 are one `optimize.py` shipped in two
   steps, so delivery stays incremental without re-introducing the
   place/route split. (needs 6)
8. **Geometric DRC on realized copper** + the O(n²) reference oracle +
   gate evaluator. Was slice 3; needs L5 to exist. (needs 7)
9. JLCPCB API client (quote/order/track). (needs 4)
10. Phase-machine gates hookup — supersedes the "route round-trip"
    wording of 0042 Slice 9; back-edges unchanged. (needs 7, 8)

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
- `src/precis/pcb/` — new: drc.py, planes.py, optimize.py, sketch.py,
  realize.py, easyeda.py, jlc_api.py, _http.py, gerber.py; extended:
  place.py (folded into optimize.py), export.py, footprint.py,
  catalog.py; eyes.py — drc_lite retired (superseded by drc.py behind
  `view='drc'`), other eyes views unchanged. No footprint_gen.py —
  footprints are pulled, never synthesized.
- `src/precis/store/_pcb_ops.py` (hydration + new row ops),
  `src/precis/handlers/pcb.py` (tool surface, job enqueue).
- Worker: new job types on the existing substrate
  (`src/precis/workers/`), route/place lanes.
- Skills: `precis-pcb-help` (+ net-class/measures cross-refs) get the
  new ops; a new `precis-route-help` runtime skill.
- Secrets vault rows (`JLCPCB_*`, set via `/secrets`) — no deploy-
  template env change needed. **No kicad-cli image dependency** (the
  KiCad-DRC oracle is dropped); Freerouting role demoted in docs.
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
- ~~Open (slice 8): which JLCPCB API auth flow~~ **Closed 2026-08-27**:
  per-request HMAC-SHA256 key-pair signing, no OAuth, no token
  endpoint — server-verified; see the auth block in Export + order.
- **Decided (user, 2026-08-27) — place and route are ONE optimizer.**
  The classic split is a workaround for maze routing (a route is
  geometry ⇒ any component move invalidates it), not a mathematical
  truth. Under a rubber-band sketch the topology is invariant to small
  placement perturbations, so joint optimization is the payoff for the
  representation we already chose. Consequences recorded in Engines:
  locality is a hard constraint on every cost term; staging survives
  only as a move-mix *schedule*; the realizer stays out of the inner
  loop; per-term/per-region decomposition is mandatory for legibility.
  Slices 6 and 7 merge into one engine shipped in two steps.
- **Decided (user, 2026-08-27) — connections carry OBJECTIVE VECTORS;
  no affinity-edge special case.** See §Connections carry objective
  vectors. Fixes a live hole: plane-served nets are removed from the
  routing objective, so a decap — whose only nets are PWR and GND —
  had **zero edges and was invisible to the optimizer**. Objectives fix
  it generally rather than by patch. Impedance objectives are
  loop-scoped (return path implied by net domain/class). Absorbs the
  trace-width policy enum. Trade-offs surface as a Pareto front over a
  few meaningful dials, not dozens of weights.
- **Decided (user, 2026-08-27) — refdes and pin maps are late labels
  with an explicit freeze at first release.** Identity is an opaque id;
  the human-facing number is assigned at export. Both are generated
  artifacts — a hand-transcribed pin map or a renumbered refdes after
  release desyncs firmware/paperwork from hardware.
- **Decided (user, 2026-08-27) — escape routing is footprint-intrinsic
  and PRECOMPUTED.** Pad gaps, shell depth and per-gap escape capacity
  follow from the footprint alone, so they are derived once per
  footprint from the parsed pads and cached in `part_footprints`,
  available at L0/L1 before any placement exists. Rotation permutes
  them; placement does not change them. Consequence worth more than the
  escape routing itself: **required layer count is derivable from
  escape demand** rather than asserted up front (~2 rings escape on top
  for 0.8 mm BGA) — which is what makes emergent layer roles workable.
- **Decided (user, 2026-08-27) — fill is a TILING, not a flood.** One
  primitive: *a net owns a region on a layer*; trace, plane, pour and
  keepout stop being different objects. Do NOT optimize the tiling
  (continuous, non-convex); **derive** it — L2 topology gives each net
  a skeleton, then a weighted-Voronoi / multi-source expansion grows
  every skeleton simultaneously with per-net weights from the objective
  vector, until clearance binds. Ground fills what remains because it
  has the most skeleton and no cap. Two mandatory checks: sliver/acute
  cull (acid traps), and **every tile must connect to its own net**
  (kills floating copper) — the plane-connectivity constraint again.
  Widened traces need thermal relief / neck-down at pads or they
  heat-sink during reflow and tombstone.
- **Decided (user, 2026-08-27) — keepouts and vias are ONE primitive:
  layer-masked obstacle regions.** A via is a small obstacle over a
  contiguous layer span that *additionally* carries connectivity; a
  keepout connects nothing. An antenna keepout must exclude the plane
  too, which holes the plane graph — the same machinery as
  antipad-induced islands, at no extra cost.
- **Decided (user, 2026-08-27) — copper balance is a CHECK, not a cost
  term** (correcting an earlier over-promotion in this same log). The
  fill/tiling pass coppers every layer to near-full by construction and
  total per-layer copper is dominated by pour, not trace width;
  residual imbalance comes from antipad/clearance density. Widening
  traces is a separate, purely positive relaxation (resistance,
  thermal, yield), bounded by the gap capacities we already compute —
  so gap capacity does double duty: constraining routability AND
  pricing the widening.
- **Decided (user, 2026-08-27) — ONE progressively-enriched IR (L0–L5),
  graph-first.** See §The IR. Work as deep in pure-graph space as
  possible and descend to geometry only when forced; each level
  decorates the previous; a move dirties only levels above it, usually
  locally. Non-negotiable corollary: **the embedding is stored
  explicitly, never derived from coordinates** — deriving it destroys
  the move-invariance the whole architecture rests on. Arrays not
  objects, so deltas vectorize.
- **Decided (user, 2026-08-27) — layers are integer indexes; names are
  export-only.** Via spans become bitmasks and the inner loop does
  arithmetic, not string hashing. `stackup jsonb` is already ordered,
  so this is discipline, not migration.
- **Decided (user, 2026-08-27) — layer ROLES are emergent, not
  hardcoded.** SIG/GND/PWR/SIG is a default, not an architecture; which
  nets become planes on which layers is a decision variable, so better
  stackups can emerge by measurement. Guarded by two cost terms rather
  than a fixed stackup: reference-plane adjacency (low weight now, high
  when RF arrives — but note it already matters for any fast digital
  edge, not just RF) and copper balance (promoted from advisory metric
  to cost term, since emergent roles can warp a panel). Layer count and
  physical stackup stay pinned by the fab menu; roles are ours.
- **Decided (user, 2026-08-27) — the ratsnest is LAYERED.** Layer
  assignment belongs inside the ratsnest, not in a pass after it,
  because a layer-free crossing is not a violation — only a same-layer
  crossing is — so the layer-free crossing count optimizes a proxy.
  Full consequences in Engines: exact gap capacity replaces RUDY once a
  sketch exists; per-gap capacity moves into the optimizer as a hard
  constraint; plane-island detection becomes a constraint rather than a
  post-hoc DRC finding; layer assignment is a third move class entering
  mid-schedule. Our own stackup reduces this to **biplanarity** (only
  F.Cu/B.Cu route), not general k-layer thickness.
- **Decided (user, 2026-08-27) — curves are arcs + tangents, not
  beziers.** The rubber-band optimum around clearance circles *is*
  tangent lines joined by arcs, in closed form; gerber has arcs
  natively and no bezier primitive. The exact answer is cheaper than
  the approximation.
- **Decided (user, 2026-08-27) — de-novo DRC against published JLC
  rules, no KiCad oracle.** The `.kicad_pcb` writer is demoted to a
  viewing convenience and `kicad-cli` leaves the image; gerber is
  written directly. Rules become versioned two-tier data (JLC minimum
  + house margin). Oracles: an O(n²) reference implementation for the
  geometry kernel, and JLC's own DFM report at slice 8. Stated
  residual gap: rule-*interpretation* error is covered by margin, not
  by an independent implementation.

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
