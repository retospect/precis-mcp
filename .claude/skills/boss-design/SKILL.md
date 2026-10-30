---
name: boss-design
description: "Use when designing screw bosses, standoffs, ribs or gussets in plastic parts (injection-molded or FDM): boss outer diameter vs screw size, pilot/core hole, gusset count/thickness/height, draft, fillets, wall distance."
---

# boss-design — bosses and gussets in plastic parts

Numbers below are for **thread-forming (self-tapping) screws into plastic**,
M4 worked example (d = screw major Ø, T = nominal wall, H = boss height).
Researched via Perplexity (Protolabs, Stratasys, FDM/IM design guides,
EJOT/REMFORM/Bossard data — `perplexity-research:526009`). **The screw
supplier's datasheet beats every rule here**; these are starting values to
validate, not standards.

## Injection-molded

| Quantity | Rule | M4 example |
|---|---|---|
| Boss OD | 2.0–2.5d (ABS/ASA 2.0d; PC 2.5d) | 8 mm ABS/ASA; 10 mm PC |
| Pilot/core Ø | 0.74–0.85d by screw family (EJOT/PT ≈0.80d; REMFORM ≈0.74–0.75d; PC ≈0.85d) | 3.0–3.4 mm |
| Thread engagement | ≈2d | 8 mm |
| Gussets (isolated boss) | 3–4 equally spaced, 0.5–0.8H | to ~0.8 of H |
| Free-standing rib height | ≤2.5–3T | — |
| Rib thickness | 0.5–0.6T (≤0.7T amorphous, cosmetic risk) | 1.5–1.8 mm at 3T |
| Draft | OD/ribs 0.5–1°/side; bore 0.25–0.5° | — |
| Base fillet | 0.25–0.5T at boss-to-floor and rib roots | 0.75–1.5 mm |
| Distance to exterior wall | ≥3.0–3.2 mm boss-OD-to-wall; connect with a rib, never merge | sink control |

Also: core the hole down to near the base-wall level (no solid plug under
the bore); add a lead-in/stress-relief counterbore at the mouth (~1.05d for
PC, which cracks at boss mouths). Validate drive torque, strip torque,
pull-out, cracking, creep, repeat assembly.

## FDM-printed

- Boss OD 2.25–2.5d preferred for structural bosses (9–10 mm for M4).
- Pilot: design to the **measured finished hole**, not the CAD value —
  FDM holes print undersized. Start ~3.2–3.4 mm actual; print a stepped
  coupon (3.2/3.3/3.4/3.5) and pick by drive/strip torque.
- Ribs 0.75–1.0T (min 1.2–1.5 mm ≈ 3–4 extrusion widths) — do NOT apply
  the molding 0.6T cap. Height 0.5–1.0H; full-height gussets are fine
  where nothing mates above them.
- No molding draft (0°); use a local lead-in chamfer.
- Fillets 0.5–1.0T (≈1T is a robust start) — larger than molded is fine,
  no sink constraint.
- Boss may merge into a side wall (no 3 mm sink gap needed).
- 4–6 perimeters / local solid infill around the hole; orient so service
  loads don't peel the boss across Z layers. For PC or frequent
  re-assembly, prefer a heat-set insert or captive nut.

## Traps

- "M4" machine thread (0.7 pitch) ≠ M4 plastic thread-formers (~1.75
  pitch) — pilot tables are not interchangeable.
- The 0.5–0.6T rib rule and the supplier boss OD can conflict (Ø3.2 bore
  in an Ø8 boss on a 2 mm floor = 1.2T boss wall). Don't thin the boss to
  please the cosmetic rule — move it off the A-surface, core the back
  side, rib it, or torque-test a reduced boss.

## In this repo (se/precis)

- `view='fasten'` stamps the pilot from the screw + strategy
  (precis-se-fasten-help) — cut what it names; the 3.55 mm stamp
  (3.3 + 0.25 print comp) matches the 0.78–0.85d band above.
- `precis.cad.inspect.thickness()` maps the material column behind a skin
  face and `sink_risks()` flags columns past `ratio × nominal` with the
  thickening leaf named as the cause — run it on the cosmetic face
  opposite any boss/rib cluster ("no solid plug under the bore" made
  probe-visible). `method='ball'` is the rolling-ball/medial measure
  (true local thickness); `'ray'` (default) is the column view. A grid
  node exactly on a wall plane stalls its ray (sdf ~ 0 along the
  march) — the probe retries it once from a laterally nudged origin; if
  it still stalls it is listed in `refused_m` (recovered nodes in
  `nudged_m`) instead of hanging; check those before trusting edge rows.
- Box2 uses: Ø8 boss (2.0d, ABS molded minimum — FDM-leaning designs
  should take 9–10 mm), 1.5 mm ribs (0.5T molded rule; take 2.25–3 mm
  for an FDM-first part), ribs to 0.82H stopping below the lid lip.
- Not yet expressible in the se DSL: draft angles, fillets (gr524645) —
  note the deviation on the design.
