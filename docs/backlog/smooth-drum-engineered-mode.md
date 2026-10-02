---
status: idea
title: smooth_drum engineered mode — exact nanotube stalk, rotationally symmetric collars
pillar: 3d-design
---

# smooth_drum engineered mode — exact nanotube stalk, rotationally symmetric collars

Reto, 2026-10-02: "Hexa Smooth Drum is awesome, but the stem is ... organic
looking, which is cool but not so stable. Ideally we can conform to a
nanotube, and a circularly symmetric structure. So it's good but we want the
engineery looking mode as well."

## Why the stem looks organic

The `smooth_drum` generator (`precis_se/atomic/generators/smooth_drum.py`)
lofts every row from the meridian (`precis_surface.rowfit`), then relaxes
with only a weak surface tether (`precis_surface.relax`, `k_surface` 0.01).
So:
- the stalk is a lofted net that approximates a (24,0) tube, not a
  (24,0) lattice;
- the defects land wherever the per-row count steps, with no rotational
  symmetry between them;
- rows are ~7-10% sparser than graphene (circumference match, see
  `precis-surface-kernel.md` slice Next), and the relax absorbs that
  unevenly.

## Engineered mode (proposal)

A `mode` param on `smooth_drum`, `"organic"` (today) or `"engineered"`:
- **Straight parts are exact tubes.** The stalk and the wall are `(n, m)`
  lattice segments from the existing tube builder (`sp2.build_cnt`), aligned
  on the axis. They are not lofted.
- **Collars are C_k symmetric.** Each transition (foot, flare, fillets)
  places its defects at a rotation order k that divides the neck count.
  For example, 6 heptagons at 60° on a (24,0) neck, and the 12 pentagons as
  two C6 rings on the fillets. Only the collar annuli are lofted, between
  fixed tube end rings.
- **The smooth meridian stays the target.** The catenoid bends and table
  fillets set where each collar sits, so the overlay and the fit
  tolerance still apply.

Existing notes to build from:
- `src/hexfold/spec.md` §28.5, "straight tubes, symmetric collars, caps
  from the cache";
- `precis-surface-kernel.md` slice "Smooth drum": the catenoid row radii,
  and the finding that a closed lid forces regular hexagonal rings;
- the graded hexfold rings (gr459928, `tests/hexfold/test_graded_bend.py`)
  as the hand-built symmetric oracle. se `hexa-nanobud-drum-graded33` is
  that style on prod, but on a (12,0) stalk.

## Acceptance (draft)

- Stalk and wall atoms sit within 0.05 Å of the ideal `(n, m)` tube
  positions after relax.
- Ring census {5:12, 7:12}, no 8-rings, θp max ≤ C60's.
- Rotating by 2π/k maps the defect centres onto each other within 0.1 Å.
- Energy per atom (Tersoff, or the relax proxy until Tersoff runs) is lower
  than the organic drum's at the same `neck`/`wall`.
- Seen in the viewer: an engineered and an organic drum side by side on
  prod (`hexa-smooth-drum-v2` is the organic reference).

test: `tests/test_se_smooth_drum_generator.py` gains engineered-mode cases.
