# pcb-0042-implementation

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
