---
status: idea
title: textile design exploration — fabric (weave/knit) kind and cut-and-sew garments in se
prio: low
pillar: 3d-design
---

# Textile design — exploration

Exploration only (Reto 2026-10-28): is textile design a fit for precis,
and where would it live. Not a spec; no slice is dispatchable. Research
source: `perplexity-research:529366` (Perplexity deep research, verify the
cited primaries before relying on any standard number). Nothing in the
repo, skills or memory graph covered textiles before this item.

## Two layers, two owners

Textile design splits into two problems that want different homes:

1. **Designing the cloth** (weaving, knitting, technical preforms) — a
   discrete topology (binary interlacement matrix, loop graph) with
   homogenised bulk behaviour. Shape of `pcb`, not `se`: a discrete
   layered design, grid DRC, fabrication export, and a separate binding
   into `se` (`pcb-se-binding.md` is the precedent). → candidate new
   `textile` kind.
2. **Designing a garment** (a shirt) — cut-and-sew: fabric is a bought
   material, the design is flat pattern pieces, seams, fit to a body and
   a cut plan. Maps onto `se`'s block/connect/measure/BOM machinery
   almost one-to-one. → `se`, riding the 2.5D sheet kernel.

Layer 1 feeds layer 2: the fabric's measured/computed properties are the
material the garment's drape simulation and marker yield need.

## The field (from the research)

No single standard defines it; it is a stack:

- **Drawings/specs:** tech pack (BOM, points of measure + tolerances,
  colourways, test schedule); fabric construction sheet; weave draft
  (threading × tie-up × treadling/lift plan → drawdown); point paper;
  Jacquard design (per-hook bitmap); knit chart (ISO 23606 symbols);
  print repeat (straight/half-drop/mirror) + colour separations; pattern
  pieces + marker/lay plan; 3D preform architecture for composites.
- **Notation standards:** ISO 3572 (weave terms), ISO 7211-1 (weave
  diagrams), ISO 4921 / 8388 (knitting), ISO 4915 / 4916 and ASTM D6193
  (stitch and seam classes).
- **Test methods (acceptance, not formulas):** ISO 7211-x, ASTM
  D3775/D3776/D3883 (construction); ASTM D5035, ISO 13934 (tensile); ASTM
  D737, ISO 9237 (air permeability); ISO 9073-9 (drape); AATCC TM8/TM61/
  TM135 and ISO 105 (colourfastness, shrinkage).
- **Formats:** WIF 1.1 (open, INI-style weave drafts); JC5/EP etc.
  (proprietary Jacquard); Stoll Sintral, Shima KnitPaint (proprietary
  knit); **knitout** (CMU, open machine-independent knitting IR); DXF-AAMA
  for pattern pieces (ASTM D6673 "DXF-ASTM" withdrawn 2019, still in CAD
  tools); TexGen `.tg3` for composite unit cells.
- **Open tools:** TexGen / pytexgen (GPL; unit cells, voxel and FE/CFD
  meshes); AdaCAD (parametric drafts); PyWeaving, `wif-weave` (WIF);
  knitout frontends, KnitScript, CMU autoknit, the Kniterate backend;
  Valentina/Seamly2D (parametric patterns). Not in the research run, from
  model knowledge, unverified: GarmentCode (ETH, SIGGRAPH Asia 2023,
  programmatic sewing patterns); KES-F/FAST as the fabric tests that feed
  cloth simulation.

The research's three-tier split maps onto our check model:

| tier | examples | precis analogue |
|---|---|---|
| exactly computable from structure | EPI/PPI, repeat, interlacements, max float, yarn length, approximate GSM, geometric cover | DRC + calc views |
| needs a material model | crimp equilibrium, bending, shear, drape, tensile, permeability, composite stiffness | se measures with a model; TexGen as external solver |
| physical acceptance test | actual GSM, D5035 tensile, D737 permeability, shrinkage, colourfastness | `measure` naming the test method; not computable |

## Layer 1 sketch — a `textile` kind

1. **Woven drafts.** Draft as matrices (drawdown is a boolean matrix
   product); views: drawdown render, DRC (float length, unbound
   ends/picks, repeat closure), calc (EPI/PPI, GSM, Peirce cover, yarn
   consumption/cost), WIF import/export. Small, deterministic, open test
   corpus (WIFStandard, public draft archives).
2. **Knitting via knitout.** Author against a virtual needle bed; DRC for
   carrier/racking/transfer conflicts; `.k` export. Kniterate is the
   accessible machine with an open backend.
3. **Technical textiles → se.** An `se` sheet/membrane block realised by
   a textile unit cell; TexGen (subprocess — GPL) returns fibre volume
   fraction, stiffness, permeability as measures. Touches the prestressed
   axial-member work: membranes and cables are one family.
4. **Reuse:** yarn/fibre properties in `material`; test methods and
   tolerances on `measure` (exact standard, edition, direction).

## Layer 2 sketch — a shirt in se

| shirt concept | se analogue |
|---|---|
| pattern piece (front, back, yoke, sleeve, collar, stand, cuff, placket) with seam allowance, grainline, notches, drills | block with a 2D sheet outline (2.5D sheet kernel, `se-off-the-shelf-fabrication.md`) |
| seam (class + stitch type + SPI) | connect between panel edges; joint class = seam class; notches = alignment ports |
| points of measure ± tolerance | measures with min/max |
| body / avatar (ISO 8559 measurement definitions) | fixture block; ease = clearance |
| grading across sizes | parameter sweep / pattern-group instancing over a size table |
| buttons, interlining, thread, labels | BOM of bought parts |
| marker (nesting on fabric width, nap, stripe matching) + DXF-AAMA cut file + tech pack | `view='fab'`; nesting is a new solver |
| drape / fit simulation | physics layer, external cloth solver fed by layer-1 fabric properties |

Genuinely new work, roughly hardest first: 3D→2D flattening
(developable surfaces, darts), cloth simulation with measured fabric
parameters, marker nesting (2D packing — kin to pcb placement).

## Open questions

- Which target first: handloom/dobby weaving (WIF), machine knitting
  (knitout), technical/composite preforms (TexGen + se), or garments
  (se panels + DXF-AAMA)? Decides slice 1.
- Garment-side research run not done yet: open pattern and cloth-sim
  tools, ISO 8559 sizing, GarmentCode's data model.
- Is there a real artefact to make (a shirt, a fabric, a preform)?
  The house rule is a reference target per campaign.
