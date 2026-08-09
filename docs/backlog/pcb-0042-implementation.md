# PCB (ADR 0042) — open slices

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

## Residuals (from OPEN-ITEMS)

- v1 done-bar (orderable board) is blocked on 3 deploy binaries: easyeda2kicad
  (real footprint conversion unwired — `src/precis/pcb/footprint.py` raises
  Unsupported without it), the Freerouting jar, and (Tier 2) kicad-cli for
  gerbers; none installed on any host.
- `deploy/roles/precis_eda` is in-repo, unapplied. Landmines: the pinned
  Freerouting 1.9.0 is coupled to `src/precis/pcb/route.py::_cmd`'s 1.x batch
  CLI (2.x reworked it — don't bump without rewriting _cmd); the jar's sha256
  pin is blank (supply-chain TODO); the DSN emits a via referencing an
  undefined padstack — check on the first real-jar run.
- Slice 3 datasheet lazy ingest, slice 8 web ratsnest SVG + BOM table, and
  slice 9 design-session orchestration (capstone): not started.
