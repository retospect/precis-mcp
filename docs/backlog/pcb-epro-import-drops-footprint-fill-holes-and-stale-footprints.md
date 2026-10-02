# pcb epro import: footprint FILL holes are dropped, and --update never refreshes a footprint

Reto on heater-base-test (2026-10-02): "CN1, CN2 look like some outline arc
has become F_Cu. C29..28 27 seem like the pads are wrong."

1. **Footprint FILL circle dropped.** CN1/CN2/CN5/CN6 use
   `SMD-1_BD8.7-D6.2`: four net-less POLYGON pads, all numbered "1", on
   TOP. Each is a quarter-ring with radii ≈3.25–4.35 mm, and all four match
   the source. The footprint also carries a `FILL` on layer 12 (MULTI), a
   circle of radius 3.2 mm, which is very likely the Ø6.4 hole the ring
   surrounds. `epro.py::extract_footprints` ignores footprint FILLs, so the
   ring renders around nothing and reads as stray copper arcs.
   *Unverified:* that EasyEDA treats a multi-layer FILL circle as a hole
   rather than copper; check against Pro before mapping it to an NPTH drill
   or cutout.
2. **`--update` keeps stale footprints.** C27/C28/C29
   (`CAP-SMD_BD5.0-L5.3-W5.3-LS6.5-FD`) have correct pads (3.5×1.1 mm at
   ±2.2451 mm, matching the source). Their stored courtyard is the pad
   extent, `bbox [-3.9951,-0.55,3.9951,0.55]`, so the silk box is an 8×1.1
   mm strip instead of the 5.3 mm can. The current reader
   (`epro.py::_footprint_courtyard`, COMPONENT_SHAPE layer 48) gives
   ±2.65 mm, but prod was imported 2026-10-01 23:13Z, before that fix, and
   `--update` does not refresh changed design-local footprints. *Unverified*
   that this is what Reto meant by "pads wrong".

Fix direction: import the FILL as a hole once its meaning is confirmed, and
make `--update` refresh a footprint whose recomputed geometry differs.

test: re-importing a footprint whose courtyard changed updates the stored
courtyard; an `SMD-1_BD8.7-D6.2` instance exports a Ø6.4 drill.
Thread: threads/pcb-easyeda-round-trip.md (import owner).
