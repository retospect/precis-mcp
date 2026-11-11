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

*(Transfer to graph memory is pending — spec-in-memory ruling
`me568168`, target carbon-topology representation group `me628774`
(the organic `smooth_drum` and its rowfit/loft machinery transfer as
kernel-doc nodes under the same gripe), gripe `gr629127`. The
2026-11-11 transfer pass had no precis MCP; the executing lane needs
the session MCP on PROD — the writes cannot be done from a no-MCP
lane. Do not delete this file until the node below is written,
`me628774`'s reading order carries its handle, `me628772`'s
not-transferred list drops this file, and the references are
repointed — retiring ahead of the transfer was reverted once already
(`a331c87a4`).)*

*Transfer manifest — coverage checked 2026-11-11 (read-only prod
queries): the authored-surface path under `me628775` (`me628788`,
`me628792`, `me628793`) already carries authored meridians, the
per-band charge census that places C_k rows of one ring, and the
tether/judge rule — the engineered mode's C_k collars are a different
claim (defects placed by symmetry on an exact lattice, not charged by
a census). `me629889` covers radius-stepped tubes (washers, reducer,
`junction.strain`); `me630422` covers caps and washer steps; the
"why the stem looks organic" diagnosis (loft + weak `k_surface`
tether, ~7–10% sparse rows) transfers with the kernel doc's nodes.
Missing — write one `section:reference` node tagged
`spec:carbon-topology` in `SPACE:repo-dev`, `part-of` → `me628774`,
`Source:` line naming this file + transfer date + gripe `gr629127`,
house shape of the `me630415`–`me630430` set:*

1. *Title ≈ "smooth_drum engineered mode: exact tube segments,
   C_k-symmetric collars". Body: Reto 2026-10-02 — the organic stem is
   cool but not stable-looking; conform to a nanotube and a circularly
   symmetric structure. A `mode` param on `smooth_drum`, `"organic"`
   (today) or `"engineered"`: straight parts are exact `(n, m)`
   lattice segments from the existing tube builder
   (`python:main::precis_se.atomic.generators.sp2.build_cnt`) aligned
   on the axis, never lofted; each transition (foot, flare, fillets)
   places its defects at a rotation order k dividing the neck count —
   e.g. 6 heptagons at 60° on a (24,0) neck, the 12 pentagons as two
   C6 rings on the fillets — and only the collar annuli are lofted,
   between fixed tube end rings; the smooth meridian stays the target
   so the overlay and the fit tolerance still apply. Build from:
   `src/hexfold/spec.md` §28.5, the kernel doc's smooth-drum slice
   (catenoid row radii; a closed lid forces regular hexagonal rings),
   and the graded hexfold rings as the hand-built symmetric oracle
   (`gr459928`, `tests/test_hexfold/test_graded_bend.py`; prod
   `hexa-nanobud-drum-graded33` is that style on a (12,0) stalk).
   Acceptance: stalk and wall atoms within 0.05 Å of the ideal (n, m)
   tube positions after relax; ring census {5:12, 7:12}, no 8-rings,
   θp max ≤ C60's; rotating by 2π/k maps the defect centres within
   0.1 Å; energy per atom (Tersoff, or the relax proxy until Tersoff
   runs) lower than the organic drum's at the same `neck`/`wall`; an
   engineered and an organic drum side by side in the prod viewer
   (`hexa-smooth-drum-v2` is the organic reference). Test:
   `tests/test_se_smooth_drum_generator.py` gains engineered-mode
   cases. `Built:` none — idea stage. `related-to` → `me628792`.*

*Then: append the new handle to `me628774`'s "Sections in reading
order" line (with this gripe's kernel nodes); edit `me628772`'s "Not
transferred yet" line to drop this file — close `gr629127` only once
both its files land — repoint
`docs/backlog/threads/hexfold-toolkit.md` Do-next 12's "Next after
it" line to the new handle; delete this file and regenerate
`docs/backlog/INDEX.md` (`scripts/docs-index`).*

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
- the graded hexfold rings (gr459928, `tests/test_hexfold/test_graded_bend.py`)
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
