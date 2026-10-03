---
status: draft
title: Ideal smooth surface first, then tile it — the surface is fixed, the tiler only follows
pillar: 3d-design
prio: high
---

# Ideal smooth surface first, then tile it

Reto, 2026-10-03 (via nanobuds-paper-25): he likes the `hexa-smooth-drum`
design, but its smooth surface looks non-ideal, as if the tiler had
co-optimised it. He wants the ideal surface defined on its own (a flat
sheet, a fillet of a chosen radius into the tube, a chosen radius into the
ball or cap), and the tiler then tiles that fixed surface. The tiler works
backwards from the ideal shape and does not bend it. This is the path to a
smooth graded flange, and the nanobuds hero (dr173020) waits on it.

## Motivation / why

The code never moves the target surface: `drum_meridian` returns a frozen
`Meridian`. Reto's reading is still right in effect, for two reasons.

- **The target's shape comes from the tiler's needs, not from authored
  radii.**
  - The foot and the flare are catenoids (zero mean curvature, chosen
    because they tile).
  - The fillet radius is the *largest icosahedral-table radius that fits*
    (`precis_surface.revolution.drum_meridian`, `rho = max(fits)` over
    `fillet_candidates`).
  - Nobody chose these radii, and the author cannot.
- **The atoms leave the target.**
  - `rowfit.realise` lofts rows about 7 % sparser than graphene.
  - The relax tether is deliberately weak (`smooth_drum._RELAX_K_SURFACE =
    0.01`), so atoms sit a mean 0.7 Å off the surface with bonds
    1.31–1.68 Å.
  - Pinning them (k 0.2, mean 0.17 Å) stretches bonds to 1.2–1.9 Å.
  - The rendered carbon is therefore a compromise surface, not the target.

The fix is to make the surface an authored, analytic object and to judge
the tiling by how closely it follows that surface, with no trade-off knob.

## In scope

1. **Surface spec (S1).**
   - Analytic meridian pieces with authored radii:
     - flat sheet;
     - torus fillet of radius `R_f` into a cylinder of a lattice tube's
       radius;
     - cylinder;
     - fillet or sphere cap of radius `R_c` into a ball or lid;
     - a stepped neck as two fillets.
   - Several axisymmetric features on one flat sheet, each with its own
     axis, so the hero scene can be written down: sheet, pill, bump, and a
     pillar with its ball.
   - A deviation metric, run on any structure: atom distance to the
     surface (mean, 95th percentile, max per region), and bond spread.
   - First use: run it on today's hero5 and on `hexa-smooth-drum-v2`, to
     put numbers on "non-ideal".
2. **Defects from the surface (S2).**
   - For each annulus, the integrated Gaussian curvature fixes the defect
     charge (Gauss–Bonnet). A sheet→tube fillet carries −2π, which is 6
     heptagons, spread over rows where the curvature accumulates.
   - Output is authored-defect lists (gr459928's format, Horizon 11's
     "solving").
   - Defects are C_k symmetric on each row.
   - Straight parts are exact `(n, m)` tubes (as in
     smooth-drum-engineered-mode.md).
3. **Tile and pin (S3).**
   - Build the hexfold net from S2, seed it on the surface, then relax it
     with a strong normal tether.
   - The surface does not move. Strain shows up in bond and angle numbers,
     not as drift off the surface.
4. **Hero render (S4).**
   - The hero scene from the S1 spec, built through S2–S3, rendered with
     the surface overlay.

## Explicitly NOT in scope

- Non-axisymmetric features (a saddle bridge between two features). Each
  feature has an axis, and the sheet is flat between them.
- Choosing the radii for the author. S1 takes them as input and reports a
  radius that cannot be tiled, rather than snapping it.
- Retiring the organic `smooth_drum` mode: it stays as the reference.

## Acceptance criteria

- S1: hero5 and `hexa-smooth-drum-v2` have per-region deviation numbers
  against an authored surface.
- S3 on one feature (sheet → `R_f` fillet → (12,0) tube → cap):
  - atom-to-surface mean ≤ 0.10 Å, max ≤ 0.3 Å;
  - bonds 1.36–1.50 Å;
  - census matches Gauss–Bonnet per annulus;
  - defects C_k symmetric within 0.1 Å.
- Changing `R_f` changes the built shape, and the deviation stays within
  the bar. That is the "tiler follows the surface" test.
- S4: the hero scene builds with no `geom.seed_overlap`, no ERROR
  `geom.clash`, and the S3 bars per feature; a render goes to the
  nanobuds-paper thread.

## Target + blast radius

- New: a surface-spec module, which may live in `precis_surface` beside
  `revolution`.
- New: the curvature→defect-row placer, which emits hexfold authored
  defects.
- Changed: hexfold's relax (stick), which gains a surface tether.
- Not touched: the organic `smooth_drum` generator.

## Risks

- S2 needs defects on tube walls and in frustum rows. gr459928 found that
  the 2+2+2 grading needs an irregular hole (3,3,6,3,3,6) that the grammar
  lacks, and that tube-wall surgery is unbuilt. If S2 cannot place a row
  where the curvature asks for it, the bar is missed there, and that shows
  up as deviation, not as a moved surface.
- A fillet tighter than about one ring row cannot be followed by any
  tiling. S1 reports the smallest radius each row pitch can follow.

test: tests/hexfold/test_ideal_surface.py (new); tests/test_precis_surface_*.py
