---
status: draft
title: atomic models as printable solids — Å→mm scale contract, fused ball-and-stick mesh, per-element colour in 3MF
prio: normal
---

# atomic models as printable solids — Å→mm scale contract, fused ball-and-stick mesh, per-element colour in 3MF

## Motivation / why

Reto, 2026-09-26: "I would like a way to scale these to 3d print size" —
"3d printing possibly colored" atomic models.

Today a structure can only be *looked* at. `precis.viz3d` renders a CPK
stick figure to SVG (`viz3d/stickfig.py`), and that is the end of the road:
the package is SVG-only by construction, and its unit contract is
display-only — it "never converts or assumes a unit, it only *displays* the
label" (`viz3d/__init__.py` docstring). So there is no path from atom
coordinates to a body a slicer will accept.

The printable half already exists, but only for the `cad` domain:
`precis.cad.export.export_mesh` folds a design with `manifold3d` and writes
watertight STL/3MF, and `se` drives it through `view='print'` with build
orientation and process DRC (`precis-se-print-help` skill). Nothing connects
`structure` to it. This item builds that bridge, at a chosen physical scale,
and gives 3MF the per-object colour it currently lacks.

Two facts shape the design:

- **Å-native source, mm-native sink.** `precis.structure` is Å-native;
  `_write_3mf` hard-codes `unit="millimeter"`. The scale factor is the whole
  feature, not an afterthought — a 3 Å bond has to become a strut a printer
  can actually lay down.
- **Colour exists, but not in the printable format.** `viz3d/stickfig.py`
  holds the Jmol CPK table (`_CPK`); `cad/gltf.py` has per-part colour for
  the *viewer*; `cad/export.py` has none. Its `_write_3mf` already emits one
  `<object>` per component, which is the natural carrier for a per-element
  colour via the 3MF basematerials extension.

## In scope

- A **scale contract**: one explicit Å→mm factor, chosen by the caller, with
  a derived print envelope reported back (bounding box in mm) so the caller
  learns the model is 400 mm wide before slicing it. Convenience framing by
  target size ("fit in 150 mm") as well as by raw factor.
- A **ball-and-stick mesh builder** in the `structure` domain: a sphere per
  atom at a per-element radius, a cylinder per bond from the existing bond
  graph, fused into ONE manifold body through the existing `manifold3d`
  path rather than a second CSG kernel.
- **Printability floors** applied at the chosen scale, as advisory findings
  (the `cad` process-DRC vocabulary already names these): strut diameter
  below a minimum extrusion width, an atom sphere that a bond no longer
  reaches, a model whose fused body is not a single connected component.
  A structure that is fine at 20 mm/Å and unprintable at 2 mm/Å should say
  so at 2, not fail silently.
- **Per-element colour in 3MF**: extend `_write_3mf` with the basematerials
  extension and one material per distinct element, reusing `viz3d`'s `_CPK`
  table. STL stays colourless (it has no notion of colour).
- The **agent surface**: reach it the way `se` reaches printing, so the same
  "mint → orient → DRC → write file" shape holds for a structure.

## Explicitly NOT in scope

- **A third CPK table.** `viz3d/stickfig.py:7` records the standing "one
  table, not a third copy" instruction; the web UI's `_CPK` is the second.
  This reuses viz3d's, it does not add one.
- **Print-in-place articulation.** No moving/rotating bonds, no gapped DOF
  joints — that machinery is `se`'s (`realize(strategy='manufacture')`) and
  a molecule is not an assembly.
- **Space-filling / van der Waals surface style, ribbons, or any second
  representation.** Ball-and-stick only; other styles are separate items.
- **Splitting an oversize model into printable chunks with registration
  features.** Report the envelope, do not solve the decomposition.
- **Multi-material machine profiles, AMS/toolhead assignment, slicer
  invocation.** Emit a coloured 3MF and stop; the slicer owns the rest.
- **Changing `viz3d`'s SVG contract or its unit-agnosticism.**

## Acceptance criteria

1. A structure with bonds exports an STL and a 3MF at a caller-given scale;
   both open in a slicer, and the 3MF's declared unit is millimetres.
2. The fused body is watertight and ONE connected component for a
   chemically connected structure — asserted on the mesh, not eyeballed.
3. The reported mm bounding box matches the Å extent times the scale factor,
   and a target-size request produces a factor whose largest dimension lands
   at that size.
4. Halving the scale factor halves every linear dimension in the written
   mesh — no hidden absolute-size constants in the builder.
5. Below the strut-diameter floor, the export surfaces an advisory finding
   naming the offending bond and the diameter it would have; above it, none.
6. A structure with N distinct elements yields a 3MF carrying N materials
   whose hex values equal `viz3d`'s CPK entries for those elements, and an
   element absent from the table takes the same `_CPK_DEFAULT` the renderer
   uses.
7. An existing `cad`/`se` export is byte-for-byte unchanged when no colour
   is requested — the basematerials addition is additive.
8. `manifold3d` absent (the `[cad-export]` extra ungated) fails with the
   same actionable `ExportError` route `export_mesh` already uses, not an
   ImportError traceback.

## Target + blast radius

- `src/precis/structure/` — the new mesh builder and the scale contract.
- `src/precis/cad/export.py` — `_write_3mf` gains basematerials; `export_mesh`
  gains the colour pass-through. This file is shared with `se`/`cad`
  printing, so criterion 7 is the guard.
- `src/precis/viz3d/stickfig.py` — `_CPK` becomes an import target (read
  only; no behaviour change).
- `src/precis/handlers/structure.py` — the agent-facing view/verb.
- `src/precis/data/skills/` — `precis-se-print-help` is at the skill size
  ceiling in places (see `multiscale-campaign-state` memory on the 32 KB
  limit); decide whether this documents into the structure skill instead.
- Post-deploy check: one real export at two scales on a prod structure.

## Open questions / decisions log

- **Scale spelling.** `scale=` as mm-per-Å, or `fit=` as a target longest
  dimension in mm, or both? Both is proposed above; one may be enough.
- **Bond strut diameter.** Derive from atom radius (a fraction of the
  smaller sphere), or an absolute mm floor at the chosen scale, or both with
  the floor winning? Affects whether a small molecule prints as spheres on
  threads.
- **Atom radius source.** Covalent radii scaled down (the usual ball-and-stick
  convention) or the renderer's existing sphere sizing? Reusing the renderer's
  keeps print and picture consistent, which is probably worth more than
  textbook proportions.
- **Route through the `cad` IR or mesh directly?** Emitting a `cad` design of
  spheres and cylinders gets fold/DRC/orientation for free but may be slow
  for hundreds of atoms; building the mesh directly is faster but duplicates
  the fuse step. Measure on a ~100-atom case before choosing.
- **Colour granularity.** One material per element is proposed. Per-atom
  colour (charge, coordination, a computed field) is a plausible later want
  and would change the object/material layout — decide now whether the
  3MF layer should be per-element or per-part-with-arbitrary-colour.
- Does an oversize model warrant a hard refusal at some multiple of a common
  bed size, or is the reported envelope enough? (Proposed: envelope only.)
