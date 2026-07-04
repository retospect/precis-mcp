# PCB / EDA (ADR 0042) — implementation tracker

> Issue tracker for building out [ADR 0042 — the `pcb` kind](../decisions/0042-pcb-kind-netlist-placement-ir.md).
> File-based (not GitHub issues) so it lives with the code and the ADR.
> One **epic** + the **v1 slices**; phase-2 + open decisions at the end.
> Check items off as they land; link the commit/PR next to the box.

**Status legend:** ☐ todo · ◐ in progress · ☑ done · ⊘ blocked

---

## Epic — `pcb` kind, JLCPCB-native EDA (v1)

**Goal:** an LLM designs a circuit, picks real JLCPCB-assemblable in-stock
parts, places them to minimize crossings, and exports a placed netlist +
BOM + CPL that **Freerouting (headless)** turns into a JLCPCB-orderable
board — all read + authored by the LLM as a traversable graph (ratsnest /
measures / signal-trace), never pixels.

**Keystone:** own the netlist + part-selection + placement IR; rent the
autorouter + gerbers + fab at export.

**v1 done-bar (decided 2026-06-28):** an **end-to-end orderable board** — one
known board (ESP32-C3 sensor node) goes design → place → export →
JLCPCB-orderable for real. So export + Freerouting are v1, not fast-follow.

**Decided 2026-06-28** (the four gating questions): (1) v1 = end-to-end
orderable board; (2) parts/footprint/pin data via **`easyeda2kicad` + the
JLCPCB parts CSV** (no paid LCSC API); (3) v1 router = **Freerouting
headless** (EasyEDA GUI-only = manual escape hatch); (4) authoring =
**records traversed as a graph** (get IC4 → pins → net per pin → reason →
hop), no DSL/import in v1.

**Slices** (each its own section below): 1 schema+handler · 2 parts catalog
+ selector · 3 `datasheet` kind · 4 the eyes (probes/trace/measures) ·
5 auto-place + round-trip · 6 exporters (BOM/CPL/netlist/mechanical) ·
7 skills · 8 web · 9 design-session orchestration (phases).

**Suggested order:** 1 → 2 → (3, 4 in parallel) → 5 → 6 → (7, 8) → 9. 1
unblocks everything; 4 needs 1; 5 needs 4; 6 needs 5; 9 ties the phases
together once they exist; 2/3 are independent of 4/5.

---

## Slice 1 — Schema + `pcb` kind handler  ☑ (core landed)
*labels: enhancement · ADR 0042 §4, §12, §14*

The relational graph + the kind that owns it. Unblocks all other slices.

**Schema proposal (review before sealing the migration):**
[`pcb-schema-proposal.md`](./pcb-schema-proposal.md) **(v2)** — full DDL for
`pcb_components`(type)/`pcb_pins`/`pcb_instances`/`pcb_nets`/`pcb_netconns`/
`pcb_measures`/`pcb_features` + `parts`/`part_footprints`/`part_availability`,
handle codes, coordinate invariants, and the catalog FK-isolation rationale.

- [x] Forward migration `0042_pcb_kind.sql` — all 10 tables + 3 kinds;
      verified applies clean to precis_test. (commit c407581)
- [x] Handle codes `pcb`→`pb`, `part`→`pn`, `datasheet`→`da` (chunk `dk`);
      `part` in `_OTHER_TABLE_KINDS`; totality test updated. (c407581)
- [x] `PcbHandler` `put`/`get`/`search`/`search_hits`/`delete` over the
      tables; design = a `refs` row + one `card_combined` chunk (auto-built
      from the graph); sub-rows addressed by design-scoped path (`slug#U3`,
      `slug@SCL`). `PcbMixin` in `store/_pcb_ops.py`; wired into dispatch.
      (commit 3661f56)
- [x] **Graph-traversal read surface** — `pcb_instance_neighbors` (the hop:
      instance → pins → net per pin → neighbour instances) + `pcb_net_members`,
      surfaced as `get('slug#U3')` / `get('slug@SCL')`. (3661f56)
- [x] **Batch `put`** — `args={components,nets,connections}` applied in one
      tx, re-runnable (existing refdes/net names reused); ad-hoc pin creation
      on wire. (3661f56)
- [x] Soft-delete via `retired_at` (cad_nodes precedent; `pcb_netconns`
      hard-delete). `fixed` mark on instances (CHECK-constrained). (3661f56)
- [x] `PartHandler` (read-only catalog) + `DatasheetHandler` (thin
      PaperHandler sibling) registered. (3661f56)
- [ ] **Active-board context** (`use pcb12` → bare `U3`) — *deferred within
      Slice 1*: needs session-param plumbing; fully-qualified paths work now.
- [ ] Derived layer (ratsnest/crossings/measure verdicts) computed-on-read
      with a per-`(ref, rev)` memo — **Slice 4** (the eyes), not Slice 1.
- [x] **DoD:** ruff+mypy clean; 9 e2e tests green (create / TOC w/ placement+
      roles / graph hop / net members / re-run / one-net-per-pin / delete).
      *(Full `boot()` + the container gate validate on a box with the `[paper]`
      extra — host boot is blocked by the pre-existing `pysbd` dependency.)*

## Slice 2 — Parts catalog + JLCPCB-native selector  ☑ (core landed)
*labels: enhancement · ADR 0042 §5*

`pcb/catalog.py` (normalizer + sqlite reader + refresh), store `parts_*`,
`pcb/footprint.py` (Flow B), `PartHandler` over the store. (container gate:
91 passed.)

- [x] **Flow A — importer**: `read_jlcparts_sqlite` + `refresh_parts_from_sqlite`
      → `normalize_jlcparts_row` (every dump row is JLCPCB-assemblable) →
      `store.parts_import` (**upsert + turnover**). Upsert (not the atomic swap)
      chosen for v1 — the swap is the scale lever for the full ~300k dump,
      noted in code. No paid LCSC API.
- [x] **Flow B — footprints (lazy, separate table)**: `ensure_footprint`
      caches in `part_footprints` (FK-free; swap never touches it), **pluggable
      fetcher** (real `easyeda2kicad` gated like cad exporters; tests inject a
      fake). *(Phase 2: internal IPC-7351 generator + datasheet pinout.)*
- [x] **Turnover signal — `part_availability`**: `parts_import` diffs each dump
      vs the previous → restock_count / trend / EWMA per C-number. Selection
      ranks on **turnover**, not instantaneous stock.
- [x] `kind='part'` handler over the catalog, addressed by LCSC C-number
      (`get(kind='part', id='C25804')`); ingest-only.
- [x] Selector `search(kind='part', q=…)`: hard-filter `jlcpcb_assemblable`,
      **rank `basic DESC, turnover DESC, ewma_stock DESC`**; shows cheapest
      unit price. (Footprint-present is enforced lazily at selection, not as a
      filter, since footprints are fetched on demand.)
- [x] Choosing a part **auto-stamps** footprint/height/courtyard onto the
      `pcb_components` row (from the catalog, by C-number).
- [x] Catalog scope: only JLCPCB-assemblable (the dump *is* that set).
- [x] **DoD:** selector returns assemblable parts Basic+turnover-first; a
      sqlite-fixture refresh populates the table; a component auto-stamps its
      footprint. ruff+mypy clean; 16 catalog tests, 48/91 pcb tests pass.
- [ ] **Deferred:** the `parts_refresh` worker daemon (per-minute rotation) +
      `precis pcb refresh-parts` CLI wiring; the real `easyeda2kicad`
      conversion (needs the dep + network in the deploy image).

## Slice 3 — `datasheet` kind (thin, capped)  ☐
*labels: enhancement · ADR 0042 §7*

- [ ] `DatasheetHandler(PaperHandler)`: `kind='datasheet'` (`da`),
      `corpus_role='evidence'`, `supports_put=False` — a ~30-line spec, the
      electronics sibling of `cfp` over the shared Marker→chunks pipeline.
- [ ] **Lazy** ingest from the catalog's `datasheet_url` (on open / on first
      design reference), not eager; `datasheet-of` / `has-datasheet` link to
      `parts` rows (many-to-many — one datasheet per part family).
- [ ] Scope datasheets out of academic `search(kind='paper')` and vice versa.
- [ ] **Cap is explicit:** one kind for the whole electronics-doc family
      (app-note/errata/ref-manual via a `meta` sub-type) — do NOT mint new
      kinds per genre.
- [ ] Track table-recognition gap (pinout / electrical-char tables) as an
      ingest improvement; prefer Octopart/Nexar structured data where present.
- **DoD:** ingest a datasheet PDF, search it, read its TOC, link it to a part.

## Slice 4 — The eyes: probes + signal trace + measures  ☑ (core landed)
*labels: enhancement · ADR 0042 §8 (needs Slice 1)*

The real payoff — how the LLM "sees" the design as numbers, not pixels.
`precis.pcb` package (pure; geom / ratsnest / eyes), fed by `pcb_graph`,
surfaced as `get(view=...)`. (commit + container gate: 74 passed.)

- [x] **Probe ladder** (§8.1): netlist TOC (Slice 1), net probe (`@NET`),
      **crossing probe** (`view='crossings'` — MST airwires + AABB-prefiltered
      crossing count, the pre-routing objective), `view='ratsnest'` (length),
      `view='proximity'`, `view='drc'` (DRC-lite). TOON output.
- [x] **Logical signal trace** (§8.2): `view='trace'` walks the netlist
      hopping **2-pin pass-throughs** (series R/C); multi-pin parts terminate
      the auto-walk (LLM supplies the datasheet hop).
- [x] **Measures** (§8.3 — the "measuring tapes"): stored via `put` (args
      `measures`), evaluated via `view='measures'`. v1 evaluators:
      `separation` / `proximity` / `height` (the placement-geometry ones);
      connectivity metrics (`parallelism`/`supply_path`/`topology`/
      `plane_continuity`/`thermal`) stored + reported `pending` until their
      evaluators land. `hard`/`soft`/`gauge` strength + `min`/`max`/`target`
      + reason; operands select by **instance** or **role class**.
- [x] **Role/class tags** on instances drive class-based measure selection
      (`{'role':'sensitive'}` resolves to all sensitive instances).
- [x] **Plane nets excluded** from the ratsnest/crossing metric (the §8.1
      derivation rule) — the netlist still models every GND/VCC connection.
- **Decision (open-Q 4) resolved earlier:** fixed library, no mini-DSL —
      datasheet + LLM judgment fill gaps.
- [x] **DoD:** crossing count + separation/height measures compute on a
      hand-built board; signal trace hops a series R; ruff+mypy clean; 31
      host tests + 74 in the container gate, all green.
- *Deferred to later slices:* density/congestion estimate + the H/V route-
      feasibility (Slice 5); the connectivity measure evaluators
      (parallelism/supply-path/plane-continuity) once placement + a coarse
      route estimate exist.

## Slice 5 — Auto-place + route-feasibility  ☑ (core landed)
*labels: enhancement · ADR 0042 §9 (needs Slice 4)*

`pcb/place.py` (pure, deterministic by seed); `put(args={'autoplace':{...}})`
runs it; `get(view='feasibility')` for the estimate. (container gate: 61 passed.)

- [x] Continuous (no-grid) placement: force-seed + **simulated annealing**;
      objective = `W_CROSS`·crossings + ratsnest length + `soft`-measure
      penalty; **`fixed` instances never move** (guarded in the placer *and*
      in `pcb_set_placement` SQL). **Translation only** at component-centroid
      granularity — rotation has no effect on the metric until pad offsets
      (Slice 2 footprints); noted in the module.
- [x] Route-feasibility estimate: H/V Manhattan split → residual same-layer
      crossings → **via estimate** (labelled "estimate, not real routing").
- [x] `put(args={'autoplace':{'iters','seed'}})` persists the result + stamps
      `meta.last_place`; reports crossings/length/objective **before → after**.
- **Decision (open-Q 2) resolved earlier:** per-measure default weights +
      LLM override; the §9 round-trip *is* the place↔route hand-off.
- [x] **DoD:** the X-board converges 1 → 0 crossings; a fixed connector stays
      put; feasibility prints vias. ruff+mypy clean; 5 pure + 3 handler tests.
- [ ] **Place↔route round-trip against Freerouting headless** — **deferred to
      Slice 6** (owns the router/export integration): place → `.dsn` →
      Freerouting → on failure re-place the hot region → re-route, bounded.

## Slice 6 — Exporters: BOM/CPL + netlist + mechanical  ☑ (core landed)
*labels: enhancement · ADR 0042 §6, §13 (needs Slice 5)*

`pcb/export.py` (pure exporters off the IR), `pcb/route.py` (Freerouting
headless wrapper + the §9 round-trip), handler `view='bom'|'cpl'|'netlist'|
'dsn'|'mechanical'|'route'`. Artifacts land under `<PRECIS_CORPUS_DIR>/pcb/
<slug>/` (or temp); the binary gate is at the **route** step only. (container
gate: 69 pcb + 43 boot/registry passed.)

- [x] BOM view → JLCPCB **BOM CSV** (`bom_csv`, designators grouped per
      distinct part); placement view → **CPL/pick-and-place CSV** (`cpl_csv`)
      carrying the one coordinate-frame conversion (`jlc_rotation` — internal
      CW-from-north → JLCPCB CCW). Unplaced / missing-LCSC parts flagged in the
      handler. *(over/under-stock + Extended-part web badge → Slice 8.)*
- [x] **KiCad netlist** (`kicad_netlist`, s-expr) **and Specctra `.dsn`**
      (`specctra_dsn`) with placement baked in — board boundary (outline
      feature or bbox), per-instance images, the network. Real Flow-B pad
      geometry where cached, a non-overlapping spread-pin placeholder otherwise
      (honest until easyeda2kicad conversion lands — Slice 2 deferred item).
- [x] **Mechanical exporter (the 0041 bridge):** `mechanical_profile` →
      board outline + mounting holes + component height-blocks (courtyard ×
      `height_mm`) as a 2.5D JSON profile a `cad` enclosure references *now*.
      Backed by a new `pcb_features` write/read path (`features` in batch `put`,
      `pcb_features_list`).
- [x] Rent **Freerouting headless** (`pcb/route.py`): `route_dsn`
      (`.dsn`→`.ses`, gated on `PRECIS_FREEROUTING_JAR` / `_BIN`, returns a
      `RouteResult` instead of raising — degrades to `.dsn`-only when absent,
      mirroring `export/compile.py`); `place_route_round_trip` is the §9
      hand-off (place → dsn → route → on incomplete re-place w/ escalating iters
      → re-route, bounded by `max_passes`). `view='route'` drives it.
      *(`.ses`→KiCad→gerbers via `kicad-cli` is the remaining deploy-wiring step
      — needs the binaries in the image.)*
- [ ] **v1 done-bar gate:** the ESP32-C3 reference board completes design →
      place → route → gerbers+BOM+CPL that are **JLCPCB-orderable** — **blocked
      on the deploy image** (real `easyeda2kicad` footprint conversion for true
      pad geometry + the Freerouting jar + `kicad-cli`). All the *machinery* is
      in and tested; the last mile is binaries/network, tracked here + in Slice
      2's deferred items.
- **DoD:** a placed board exports a valid BOM+CPL+netlist+DSN; outline+holes
      consumable by a `cad` enclosure; the round-trip drives Freerouting (or
      degrades cleanly). ruff+mypy clean; 21 export/route + 5 handler tests, all
      green in the container gate.

## Slice 7 — Skills  ☑ (core landed)
*labels: documentation · ADR 0042 §14*

Eight cross-linked skill files under `src/precis/data/skills/`. Auto-discovered
+ searchable (file-backed `FileCorpusIndex`); the index gates each on its
subject kind being registered — all 8 read AVAILABLE once pcb/part/datasheet
are wired. (container gate: skill suites green; `_availability_gap` = None for
all 8.)

- [x] `precis-pcb-help` (verbs + graph model + the place/export loop + a
      canonical end-to-end scenario + the coordinate frame).
- [x] `precis-part-select-help` (selector + Basic/Extended + **turnover** stock
      policy + the footprint auto-stamp).
- [x] `precis-net-class-help` (name + classify nets from datasheet + circuit
      reasoning; drives width-from-current / planes / measure defaults; plane
      ratsnest-exclusion explained).
- [x] `precis-measures-help` (the measuring-tape library: separation/proximity/
      height now + the pending connectivity metrics; role-class selection;
      hard/soft/gauge).
- [x] Domain skills: `precis-decoupling-help`, `precis-i2c-help`,
      `precis-spi-help`, `precis-datasheet-help` (pattern playbooks that map
      straight onto the `put` surface + cross-link the core skills).
- [x] **Discoverability (revised 2026-06-30):** `pcb` gets **one line in
      `precis-overview`** (mirroring the `cad` precedent — modest cost, points at
      `precis-pcb-help` + `kind='part'`/`'datasheet'`). The deep skills
      (part-select / net-class / measures / the playbooks) stay **skill-search
      only** — discoverable via `search(kind='skill', q='pcb'/'circuit'/
      'footprint'/'decoupling'/'i2c'…)` and the kind-gated index, not in the
      always-loaded catalog. A canonical PCB scenario lives in `precis-pcb-help`
      itself; the cross-kind toolpath index is `precis-toolpath-help` (its
      Authoring-artifacts row points here for the deep PCB surface).
- **DoD:** 8 skills served + searchable + not gated; one `pcb` row in
      `precis-overview`; a PCB scenario shipped (in `precis-pcb-help`). Skill
      suites green (the arbitrary ≤100 count cap bumped to ≤200 for the +8).
- *Deferred:* a formal ADR-0032 skill **group** / ADR-0038 conditional module —
      no such machinery exists in this build (skills are plain file-backed +
      content-searched); the keyword-in-summary + kind-gate path satisfies the
      decision's intent. Revisit if a real group construct lands.

## Slice 8 — Web  ☐
*labels: enhancement · ADR 0042 §14*

- [ ] Ratsnest **SVG** (airwires + crossings highlighted + active measuring
      tapes) — the primary view; exact straight-line geometry, not a render.
- [ ] BOM table + probe/DRC results panel; `fixed` nodes marked 📌.
- [ ] Optional human board viewer (vendored, like pdf.js); agent never needs it.
- **DoD:** a board renders its ratsnest + BOM in the web UI.

## Slice 9 — Design-session orchestration (phases)  ☐
*labels: enhancement · ADR 0042 §15 (needs Slices 1–6)*

A board is built as **ordered, gated phases** on the existing `plan_tick`/job
substrate — framework owns the state machine + gates, the LLM (per-phase
skill) owns the decisions. **Not** a free-running skill.

- [ ] A `pcb` design = an `LLM:*` **project** (todo + `meta.workspace`); each
      phase a child; the planner coroutine sequences them.
- [ ] Phases: 1 intent/requirements · 2 architecture+datasheets · 3 netlist
      +net-classes · 4 part-selection · 5 placement · 6 route round-trip ·
      7 export/order.
- [ ] **Gates** as new `auto_check` evaluators: `netlist_drc_clean`,
      `all_parts_selected`, `placement_legal`, `route_complete`.
- [ ] **Back-edges** (not a one-way pipeline): 6→5 (the §9 shove round-trip),
      5/4→4 (different part), any→3 (netlist wrong).
- [ ] Concurrency is solved *by phasing* — netlist-edit (3) and placement (5)
      are never concurrent; no locking beyond §12 row-level `FOR UPDATE`.
- **DoD:** the ESP32-C3 reference board runs end-to-end through the phase
      machine, gating at each step, looping 6→5 on a routing failure.

---

## Phase 2 (not yet sliced — see ADR §13a, Phasing)

- **The shove router** — precis-owned coarse maze/rubber-band router with the
  LLM-piloted place↔route shove loop; measures as cost function; **own
  routing, rent gerbers/DRC** (emit KiCad, let `kicad-cli` DRC + write
  gerbers). ~4–8 focused weeks; honest hard edges = BGA escape, high density,
  controlled-impedance, pour DRC.
- Length-matching / differential-pair measures (real routed-length math).
- Richer net-current estimation beyond LLM-from-datasheet.
- Richer datasheet table extraction.
- Full 0041 enclosure bridge (3D component models, not just height blocks).

## Decided 2026-06-28

- **v1 done-bar** = end-to-end JLCPCB-orderable board.
- **Parts/footprint/pin data** = `easyeda2kicad` + JLCPCB parts CSV (no paid
  LCSC API).
- **v1 router** = Freerouting headless (EasyEDA = manual escape hatch).
- **Authoring** = records traversed as a graph for *read*; **batch `put`**
  (list args, no DSL/format) for *write* — never one put per row. (Slice 1.)
- **Net current/class** = from datasheet + general reasoning, via the
  `precis-net-class-help` skill; explicit override allowed.
- **Stock → turnover** = rank on a derived `part_availability` score (restock
  trend from daily dumps), not instantaneous/live stock — avoid the last-reel
  part. No live scrape; JLCPCB order is final gate. (Slice 2.)
- **Footprints = tiered** = v1 rent `easyeda2kicad`; phase-2 internal IPC-7351
  generator (standard packages) + datasheet pinout; fetch-fallback for the
  tail. (Slice 2 / Phase 2.)
- **Discoverability** = EDA kinds context-gated, NOT in the global catalog;
  conditional module (0038) + EDA skill group (0032). (Slice 7.)
- **Handle codes** = `pcb`/`pb`, `part`/`pn` (other-table, C-number-addressed),
  `datasheet`/`da` (chunk `dk`). (Slice 1.)
- **Schema micro-decisions** = free-text+enum for class/metric/ftype;
  `pcb_netconns` hard-delete; `stock integer`; `part` in `kinds`. (proposal.)
- **Measure expressiveness** = fixed library, **no mini-DSL** — datasheet +
  LLM judgment make the "acceptable" call. (Slice 4.)
- **Soft-measure weights** = per-measure-type **defaults + LLM override**; the
  §9 round-trip *is* the place↔route hand-off (no threshold). (Slice 5.)
- **Addressing** = `pb12#U3` (the global handle) + an **active-board context**
  (`use pcb12` → bare `U3`/`SCL`); no per-row integer handle. (Slice 1.)
- **Orchestration** = ordered **gated phases** on the `plan_tick`/job
  substrate (framework state-machine + per-phase LLM skill), **not** a
  free-running skill; concurrency solved by phasing. (Slice 9, §15.)
- **Layers** = **4-layer default `Sig/GND/PWR/Sig`** (planes inner,
  components+signals outer — manufacturable; **confirmed** over a
  power-top/ground-bottom stripline stack); H/V routing on the two signal
  layers. 2-layer supported; **1-layer (MCPCB) future** — crossing-minimizer
  doubles as jumper-minimizer.
- **Parts import** = two flows: **A** bulk catalog from the `jlcparts` dump
  via staging-table + atomic swap (drop-index trick optional); **B**
  footprints lazy via `easyeda2kicad`, cached in a separate `part_footprints`
  table the catalog swap never touches. Bulk cadence daily/weekly.
- **Lower-stakes defaults** (chosen unless overridden): catalog scope =
  JLCPCB-assemblable only · DRC-lite = hardcode JLCPCB's capability matrix ·
  placer/router run as a `job` (async).

## Cross-cutting decisions still open

- **Refresh cadence** for `parts_refresh` (minor; daily matches `jlcparts`).
- **Router cost-function tuning** — phase 2 only, when we own a router
  (ADR open-Q 1).

*(A stock→turnover, B batch-write, C walking-skeleton, D 4-layer, E tiered
footprints — all resolved 2026-06-28; see Decided above. Schema ready to
seal pending a final read of `pcb-schema-proposal.md`.)*
