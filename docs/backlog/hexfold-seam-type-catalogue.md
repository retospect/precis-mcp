---
status: draft
title: hexfold seam-type catalogue — a closed set of supportable seam geometries, keyed by (k, dihedral pattern, rim edge-word, hybridisation)
prio: normal
model: opus
---

# hexfold: seam-type catalogue

Reto, 2026-09-26: "I just feel there are certain seam types we can
support, so we should have a ... class of those (120-120 vs 4x'90', vs
120)". And the scope ruling in the same exchange: **"joining 3 sheets
unequally we just don't do yet."**

Not blocked. This is design work that lands *into* `src/hexfold/spec.md`
§6.2/§6.4/§11.3; `hexfold-integration` roadmap item 2 (`seam` k ≥ 3) is
the consumer and wants this decided before it builds.

## Motivation / why

§11.3 admits `seam … k≥3 atoms=sp2` and defines the seam atom as one atom
bonded to the i-th dangling atom of *every* participating rim. A trivalent
atom serves exactly three rims, so `k=4 atoms=sp2` is grammatically legal
and arithmetically impossible, and §6.4 consequently exiles every
four-sheet case to the sp³ item. Meanwhile §22.2's bent-seam solver does
not exist, and today an asymmetric case is "solved symmetrically, with no
`fit.unsolvable` and no warning" — silently wrong output rather than a
refusal.

A closed catalogue fixes both: it says what the notation supports, and it
makes everything else an explicit refusal. It is also the shape hexfold
already uses — §7's primitive table is a closed catalogue of decorated
lattice patches, and §12.1 already has a families interface.

## The geometry, corrected (verified 2026-09-26)

The seam atom's bond into a sheet lies in that sheet's plane but is **free
to tilt within it**. Fixing it perpendicular to the seam line was an
unstated assumption in `hexfold-sp3-seam.md` (now corrected there). For
dihedral φ between two half-planes and tilt α from the seam axis, two
bonds on opposite sides along the axis subtend

    cos θ = −cos²α + sin²α · cos φ

Verified by direct computation (pure trigonometry, no census involved):

| config | α | bond angles |
|---|---|---|
| 4 planes at 90°, alternating up/down | 54.74° = arccos(1/√3) | 109.47° ×6 — **exactly** tetrahedral |
| 4 planes at 90°, alternating up/down | 60° | 104.48° ×4, 120° ×2 |
| 4 planes at 90°, all same side | 60° | 75.52° ×4, 120° ×2 — bad |
| 4 planes at 90°, perpendicular bonds | 90° | 90° ×4, 180° ×2 — the old wrong answer |
| 3 planes at 120°, perpendicular bonds | 90° | 120° ×3, coplanar — §6.2's sp² model |
| 3 planes at 120° | 60° | 97.18° ×3, **not coplanar** — not sp² at all |

**α is set by the rim edge-word, not chosen.** Measured off a flake by
taking each degree-2 rim atom's dangling direction against the rim line:
zigzag rim → α = 90°; armchair rim → α = 60°. The lattice therefore does
not offer the exact-tetrahedral 54.74°, but armchair's 60° lands ~5° off
on four bonds, which is ordinary strain rather than a degeneracy.

**The load-bearing consequence: seam type and rim edge-word are one
decision.** Three sheets at 120° require zigzag rims (armchair gives
97.18° and non-coplanar bonds, so it is not sp²); four sheets at 90°
require armchair rims (zigzag collapses to 90°/180°). This is strictly
stronger than §11.3's present rule, which only requires the k rims to have
edge-words compatible with *each other* and says nothing about matching
the seam's dihedral pattern.

## In scope — the catalogue

Each entry is keyed `(k, dihedral pattern, rim edge-word, hybridisation)`
and owes a seam-atom motif, a ring census and a strain figure.

| k | dihedrals | rim | seam atoms | status |
|---|---|---|---|---|
| 2 | crease | either | — | not a seam; `fuse` merges to one sheet (§6.4). Implemented |
| 3 | 120·3 | zigzag | 1 × sp² | §6.2's model. Unstrained, coplanar, trivalent. Census **derived only** |
| 4 | 90·4 | armchair | 1 × sp³ | un-excluded by the correction above. Census + strain owed |
| 4 | 90·4 | armchair | 2 × sp², bonded to each other, 2 sheets each | Reto's third option; had no home anywhere before this item |
| 3 | unequal | — | — | **OUT — "we just don't do yet" (Reto, 2026-09-26). Refuse, do not approximate** |

- A `seam.type` selector (or inference from the k rims' edge-words, with
  the mismatch as an ERROR) so an author cannot silently get the wrong α.
- **Refusal is a deliverable.** Anything off-catalogue — notably an
  unequal-dihedral k=3 seam and any k ≥ 5 — must produce
  `fit.unsolvable`/`port.mismatch`, never a symmetric guess. This
  supersedes §22.2's current silent behaviour *for seams* (the collar case
  stays §22.2's).

## Open questions / decisions log

1. **Ring census per entry — Reto, 2026-09-26: "the ring census we need to
   think about it."** Deliberately unresolved here; no number is asserted.
   §6.2 derives the k=3 sp² census as octagons in three families (AB, BC,
   CA) with the note "derived here, **verify by build**", and §29 Q6 already
   flags it as unchecked against carbon honeycomb [S2]. The k=4 entries have
   no census at all. Nothing downstream should quote a census until one is
   built and checked.
2. **Does the two-sp²-carbon motif keep §6.3's per-sheet χ rule?** With two
   seam atoms per period instead of one, the seam-face enumeration changes;
   whether seam faces still "belong to no sheet" needs re-deriving.
3. **What does "2 sheets each" mean** — four distinct sheets, or two
   carbons sharing sheets? Changes the census, the coplanarity of the two
   carbons' bonds, and the motif's dihedral. Pin this first; everything in
   that row depends on it.
4. **Is the squashed-tetrahedron 104.48°/120° within `strain_max`?** Ties
   to §29 Q5 (the `strain_max` number is a literature lookup).

## Explicitly NOT in scope

- Unequal-dihedral k=3 seams (ruled out above).
- Seam **vertices** — points where seam curves meet. Stays excluded per
  §6.4 and owned by `hexfold-sp3-seam.md`.
- The sp³ *isolation band* — `hexfold-sp3-isolation-band.md`, decoration
  within one sheet, unrelated.
- Any electronic-structure claim.

## Acceptance criteria

- Each catalogue entry builds and `check`s clean, with per-sheet
  `euler.residual` 0 for every participating sheet (§6.3).
- Every seam atom reports the bond count and hybridisation its row
  declares, and its measured bond angles match the table above within the
  relaxer's tolerance.
- An unequal-dihedral k=3 seam and a k=5 seam are both **refused** with a
  named code, and a test pins the refusal.
- A rim edge-word that contradicts the seam type is an ERROR, not a
  warning.
- The k=3 sp² census is checked against carbon honeycomb [S2] before any
  entry's census is treated as truth (§29 Q6).

## Target + blast radius

`src/hexfold/spec.md`: §6.2 (the α freedom + the edge-word coupling),
§6.4 (four-sheet seams are no longer excluded on geometry grounds — only
seam vertices are), §11.3 (`seam.type`, the strengthened edge-word rule,
the k ≥ 4 semantics), §29 (retire the fixed-α premise; Q6 unchanged).
Then `text.py` (selector), `build.py` (motifs), `check.py` (refusal
codes). No precis-side change.
