---
status: idea
pillar: 3d-design
title: se viewer — click atoms or select a region and see the DFT band structure projected onto them
---

# se viewer: band structure and projected DOS for clicked or region-selected atoms

Reto, 2026-10-09 (via chat-interface): "if DFT has been run, I want to
click on atoms (or bulk select regions), and see the band structures for
these atoms." Sibling of
[se-viewer-electronic-and-strain-fields](se-viewer-electronic-and-strain-fields.md)
(tight-binding, no DFT, per-atom colour); this item is the DFT-backed
view on a selection.

## What "band structure for these atoms" is

A band structure E(k) belongs to the periodic cell, not to an atom. What
is per-atom is the projection of each state onto that atom's orbitals,
so the view on a selection is:

- **Projected DOS (PDOS)** of the selection, E − E_F on the axis, per
  spin when the run is spin-polarised, total DOS greyed behind it.
- **Fat bands**: the cell's E(k) along the standard path, each point
  drawn with a weight equal to the selection's projection on that state.
  A tube is periodic in one direction (Γ–X along the axis); a sheet in
  two (Γ–K–M–Γ); a non-periodic composite has no k, so only the PDOS and
  the discrete level list are shown, and the panel says so.
- Scalars beside the plot: E_F, the gap (cell and selection-local, the
  latter from the PDOS edges), the selection's share of the states in a
  ±0.5 eV window around E_F.

## In scope

1. **Persist what the plot needs.** `src/precis_dft/_container/gpaw_relax.py`
   writes `result.json` with scalars only (`E_tot`, `max_force`,
   `converged`, `fermi`, `magmoms`); the calculator state is discarded.
   After a converged relax, also write the per-atom orbital projections
   of every state (atom × band × k × spin weights, plus the eigenvalues
   and k-path), compact (float16 npz or equivalent), as a job artifact
   the `dft_calculation` record links. Optional: keep the `.gpw` as a
   blob for later re-projection. A bands pass on a tube or sheet along
   the standard path is a second, explicit step with its own cost line;
   the relax's own k-mesh gives PDOS without it.
2. **Selection in the viewer.** The structure page uses 3Dmol
   (`src/precis_web/static/3dmol/`, wired from
   `src/precis_web/routes/structure.py`), which supports clickable atoms
   and selections. Click toggles an atom; shift-drag or a lasso bulk
   selects; a selection is an `atoms:<block>[…]` region (the selector
   `precis-se-regions-help` already accepts and does not yet resolve),
   and "name this selection" saves it as a pocket so the same atoms can
   be re-plotted after a re-run.
3. **Panel.** Beside the viewer: PDOS and fat bands for the current
   selection, redrawn on selection change from the stored projections
   (no DFT call in the request path). No DFT on the block → the panel
   offers the tight-binding fields from the sibling item instead and
   says they are not DFT.
4. **API.** `get(kind='se', id=…, view='bands', args={'atoms': [...]})`
   (or a region selector) returns the same numbers as JSON so an agent
   can ask "what is on the seam atoms" without the viewer.

## Explicitly NOT in scope

Running DFT from the viewer; Wannier or unfolding for supercells
(supercell bands are folded and the panel says so); Bader charges or
STM simulation; anything for runs that did not converge.

## Why

The tube item decides metallic versus semiconducting from (n,m) and the
tight-binding item estimates where states sit; this is the ground truth
on the built atoms once a DFT run exists, and the selection is the
question Reto asks of a composite: "what do the states on the seam, the
fin, the capped end look like". Persisting projections is cheap next to
the run that produced them and is the only part that cannot be added
later.

test: a converged (6,6) tube run yields a `projections` artifact; the
`bands` view on all atoms reproduces the metallic crossing at E_F; on a
(10,0) tube it shows the gap; selecting the six seam-ring atoms of a
5-7 seam gives a PDOS peak inside the pristine gap; an `atoms:` selector
out of range is a typed refusal; a block with no DFT run returns the
tight-binding fallback flagged as such.
