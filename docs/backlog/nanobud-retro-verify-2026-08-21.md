---
status: draft
title: "retro-verify of 239 withheld nanobud evidence edges — 209 released, 30 rejected, 6 hubs stranded"
---

# The edges that were never verified at all

Run 2026-08-21 with 12 opus-5 agents over every **withheld** evidence edge on
the 126 nanobuds claim hubs — 239 edges across the 108 hubs that had any, the
ones `nanopub.preflight` blocks because `meta->>'support' IS NULL`. (Two further
`contradicts` edges are excluded: preflight blocks those outright, and a verify
verdict must never be able to release a live dispute.) Companion to
`nanobud-grounding-audit-2026-08-20.md`, which read claim-vs-passage on *cited*
hubs; this one clears (or refuses to clear) the publication gate itself.

Each agent judged under the exact `_chase_llm._PROMPT_VERIFY` contract, with the
grounding chunk as the only admissible evidence. Verdicts were applied to prod:
209 edges now carry `support` + `caveats` + `verified_by: opus-5/retro-verify`.

## Result

| | count |
|---|---|
| `yes` — chunk states the claim | 52 |
| `partial`, scoped (releases) | 157 |
| `no` (withholds) | 30 |
| of which contradicts | 1 |
| **released / still withheld** | **209 / 30** |

**12% of the evidence backing this paper does
not support its claim.** Those edges were attached by semantic similarity, never
verified, and were one `nanopub publish` from being signed and OTS-anchored.

## The apply is asymmetric, deliberately

`preflight.withheld_edges` gates on `meta->>'support' IS NULL`, so writing **any**
support value releases the edge. A rejection must therefore NOT be written back as
`support:"no"` — that would publish exactly what the pass just rejected. Rejected
edges were left untouched and are listed below. Anyone running this pass on another
claim set must know this before writing anything.

Undo for the 209: `meta - 'support' - 'caveats' - 'support_reason' - 'verified_by'
- 'verified_at'` over the applied link_ids (recoverable from
`meta->>'verified_by' = 'opus-5/retro-verify'`).

## Needs a human

### Stranded hubs — every evidence edge rejected

Removing the bad attaches leaves the claim with nothing. Each needs a new source or
withdrawal from the paper:

- `fi189549`
- `fi191000`
- `fi191014`
- `fi191021`
- `fi191138`
- `fi191167`

### A claim its own citation contradicts

`fi191015` asserts:

> Local curvature and structural defects in the graphene lattice lower the fusion reaction barrier between C₆₀ and graphene.

Its cited source (*Impact of Local Curvature and Structural Defects on Graphene–C60 Fullerene Fusion Reaction Barriers*) reports the opposite — Runs counter to the claim: after computing potential energy surfaces for the defect-containing SLG models, "the calculations shows that in all considered cases the fullerene avoids the area where the defect is located", i.e. defects repel rather than facilitate the fusion.

This is a claim-authoring fix, not an edge removal.

### A claim shape that can never verify

`fi189549` reads *"The synthesis, properties, structural peculiarities, and
applications of nanobuds… **have been surveyed**, combining experimental
observations with density-functional-theory predictions."* All three of its
sources are individual studies, and none could be a survey. The claim was
extracted from a review paper's abstract sentence *about itself*, so it asserts
something about the literature rather than about nature — unverifiable by this
machinery no matter how many sources are attached. **Worth sweeping the corpus for
other hubs of this shape before any sign-off.**

### Conjunctive claims (not blocking, but decide before signing)

157 of 239 verdicts were scoped `partial`, and the
reason was consistent across batches: the hub claims are *conjunctions* and no
single paper covers the whole conjunction. Only 40 of 108 hubs have even one clean
`yes`; 62 have nothing but scoped partials. They pass the gate (`is_corroborating`
admits a non-contradicting partial) and will publish with every citation caveated.
That is a claim-shape problem the taproot extractor created at authoring time.

## The 30 rejected edges

### fi189549 — **STRANDED**

> The synthesis, properties, structural peculiarities, and applications of nanobuds and closely related hybrid nanostructures have been surveyed, combining experimental observations with density-functional-theory predictions.

- link `992062` — *The local study of a nanoBud structure*
  - The chunk is the conclusion of a single experimental TEM/STM/Raman study of one NanoBud with (16,11) chirality; it is not a survey of synthesis, properties and applications and contains no density-functional-theory predictions.
- link `992063` — *Understanding the Interaction Between Fullerene and Graphene Nanoribbons Using Density Functional Theory*
  - The chunk reports one DFT study of B- and N-doped C60-ZGNR nanobud stability and bond lengths; it neither surveys the field nor combines experimental observations with the calculations.
- link `992064` — *Interaction between nanobuds and hydrogen molecules: A first-principles study*
  - The chunk describes a first-principles H2 adsorption-energy calculation on nanobuds - a single focused computational study, not a survey of synthesis, properties, structural peculiarities and applications.

### fi191000 — **STRANDED**

> Spin-polarized DFT-D3 calculations including pairwise van der Waals dispersion find carbon nanobud formation on carbon nanotube sidewalls endothermic, with C60 binding energies of +0.736 eV on the (10,0) tube and +0.685 eV on the (5,5) tube, a sidewall formation barrier of 1.92 eV and dissociation b

- link `992119` — *Supplementary Information*
  - The table lists cap bonding geometries with binding energies of -0.276, 0.210, -0.200, 0.514 ... 2.843 eV; none of the claimed values (+0.736/+0.685 eV sidewall binding, 1.92/0.77 eV sidewall barriers, 1.52/1.46 eV cap barriers) appear, and the chunk reports no barriers, no (10,0)/(5,5) results, and no DFT-D3 or spin-polarization details.

### fi191014 — **STRANDED**

> The original aerosol CVD process yields a broad distribution spanning 0.7–2 nm

- link `994387` — *Investigations of NanoBud formation*
  - The chunk reports fullerene diameters "in the size ranging from 0.4 to 2 nm" measured when 145 ppm H2O vapor was introduced in the reactor, not the 0.7-2 nm distribution of the original process that the claim names.

### fi191021 — **STRANDED**

> First-principles calculations converge on a picture in which the band gap of a carbon nanobud is controlled largely by the chirality of the host nanotube.

- link `994390` — *First-principles study of the structural, energetic and electronic properties of C<sub>20</sub>-carbon nanobuds*
  - The chunk attributes gap control to a different variable than the claim: "the band gaps can be tuned by controlling the density of adsorbed C20", with no comparison across host-nanotube chiralities.
- link `994393` — *Electronic and magnetic properties of small fullerene carbon nanobuds: A DFT study*
  - The table lists Egap only for Cn-(5,5) nanobuds, varying fullerene size and cycloaddition configuration at a single host chirality, so it cannot show that chirality is the controlling factor.
- link `994394` — *Electronic and magnetic properties of small fullerene carbon nanobuds: A DFT study*
  - The table lists Egap only for Cn-(5,0) nanobuds, and within that single chirality the gaps still vary widely with fullerene size and configuration (0.07-0.32 eV); no chirality comparison is made in this passage.

### fi191138 — **STRANDED**

> Dispersion-corrected DFT (PBE-D3) calculations with AIM and energy-decomposition bonding analysis find that fullerene bonding on pristine graphene is dominated by weak electrostatic interactions, whereas adsorption onto Fe-doped graphene forms markedly stronger covalent bonds.

- link `992148` — *Chemical and Physical Viewpoints About the Bonding in Fullerene–Graphene Hybrid Materials: Interaction on Pristine and F*
  - Only sets up the analysis ('The chemical perspective ... was studied by the AIM and NBO schemes') and captions Figure 3; it reports no electrostatic-versus-covalent result for pristine versus Fe-doped graphene.
- link `992153` — *Chemical and Physical Viewpoints About the Bonding in Fullerene–Graphene Hybrid Materials: Interaction on Pristine and F*
  - Describes the ALMO-EDA decomposition terms and points to Table 2/Figure 4 but reports no values or comparison, so it does not establish weak electrostatic bonding on pristine versus stronger covalent bonding on Fe-doped graphene.

### fi191167 — **STRANDED**

> B3LYP and dispersion-corrected B3LYP-D3 calculations on nanobuds designed from C20 and C60 fullerenes find HOMO-LUMO gaps of 1.7 eV and 2.14 eV for structures A and B, narrowing to 0.76 eV and 1.11 eV under the D3 correction, below the 4.41 eV and 1.92 eV of pristine C60 and the 3.74 eV and 2.74 eV 

- link `994401` — *Design and computational study of the novel nano-buds of C20@C60 with high NLO properties*
  - The chunk is entirely about electric charge transfer from C60 to C20 and the resulting dipole moment in structures A and B; it reports none of the claimed HOMO-LUMO gaps (1.7, 2.14, 0.76, 1.11, 4.41, 1.92, 3.74, 2.74 eV) and no B3LYP versus B3LYP-D3 comparison.

### fi189524

> Hybridization of fullerenes with two-dimensional nanomaterials improves the physical and chemical properties of the 2D host

- link `993366` — *Design and analysis of sandwiched fullerene-graphene composites using molecular dynamics simulations*
  - The passage only establishes thermodynamic stability of the atomistic models and prospective manufacturability ("manufacturing of the proposed FG material systems looks like attainable in the future"); it reports no physical or chemical property of the 2D host being improved.

### fi189548

> Spin-polarized density-functional calculations predict that all-carbon nanobuds carry a substantial density of unpaired spins from carbon radicals created by geometry-induced electronic frustration in the region connecting the fullerene to the nanotube surface, giving net magnetic moments of 6.0 μB 

- link `993372` — *Magnetic properties of all-carbon graphene-fullerene nanobuds*
  - This chunk concerns graphene-C54 nanobuds and only tabulates NM/AFM/FM energy differences and a binding energy - a different system with no unpaired-spin-density mechanism and none of the claimed 6.0/4.25 mu_B moments.

### fi190976

> Continuous aerosol (floating-catalyst) chemical-vapour-deposition synthesis from ferrocene vapour decomposition in a carbon-monoxide atmosphere demonstrates the formation of carbon nanobuds in a single continuous reactor, with the maximum fullerene coverage of the nanotube surfaces reached at a reac

- link `992128` — *A novel hybrid carbon material*
  - The chunk is an STM electronic-states argument for "the covalent nature of the fullerene-SWNT bond"; it says nothing about aerosol CVD synthesis, ferrocene/CO, a continuous reactor, or a temperature optimum.
- link `993378` — *Investigations of NanoBud formation*
  - The chunk only reports that in situ sampling was done 'at the set temperature of 1000 C, corresponding to the maximum reactor temperature of 1058 C' with 125 ppm H2O; it says nothing about ferrocene, a CO atmosphere, nanobud formation, or fullerene coverage peaking at 1000 C.

### fi190978

> Fullerene nucleation is favoured at defect sites on the CNT surface

- link `992112` — *A novel hybrid carbon material*
  - The chunk reports DFT energetics of fullerenes ester-bonded to single-vacancy SWNTs and states those configurations 'are metastable with respect to forming perfect tubes together with oxidized fullerenes' — it addresses post-formation stability, not whether nucleation is favoured at defect sites, and involves no in situ sampling.

### fi191009

> Wet-chemical 1,3-dipolar cycloaddition (the Prato reaction) on a graphene–C₆₀ nanobud hybrid demonstrates that organic functionalisation occurs selectively on the fullerene cages while the graphene sheet itself stays unfunctionalised, and that the attached hydrophilic groups markedly increase the di

- link `993379` — *Graphene nanobuds: Synthesis and selective organic derivatisation*
  - This is a bare synthesis recipe (20 mg pG-C60 with 3,4-dihydroxybenzaldehyde and sarcosine in DMF, heated 3 h, filtered, re-dispersed in water); it neither demonstrates selectivity toward the fullerene cages nor reports any dispersibility result.

### fi191011

> Potentiostatic electrochemical treatment of graphene films in an aqueous 0.5 mol L⁻¹ H₂SO₄ electrolyte demonstrates in situ growth of carbon nanobuds on the graphene sheet surface between 1.4 V and 2.0 V, the buds growing larger and forming carbon nanoballs as the applied constant potential increase

- link `992143` — *<i>In situ</i> growth of novel carbon nanobuds and nanoballs on graphene nanosheets by the electrochemical method*
  - Chunk only describes the Raman D and G peaks near 1350 and 1580 cm-1 and the general utility of Raman for graphene; it makes no statement about nanobud growth, potential dependence, or nanoball formation.

### fi191015

> Local curvature and structural defects in the graphene lattice lower the fusion reaction barrier between C₆₀ and graphene.

- link `992140` **[CONTRADICTS]** — *Impact of Local Curvature and Structural Defects on Graphene–C60 Fullerene Fusion Reaction Barriers*
  - Runs counter to the claim: after computing potential energy surfaces for the defect-containing SLG models, "the calculations shows that in all considered cases the fullerene avoids the area where the defect is located", i.e. defects repel rather than facilitate the fusion.

### fi191132

> Geometric construction shows that inserting a single pentagon–heptagon defect pair into an otherwise perfect hexagonal carbon-nanotube lattice preserves the threefold coordination of every carbon atom while accommodating a change in tube radius or chiral indices across the junction.

- link `994392` — *Pure Carbon Nanoscale Devices: Nanotube Heterojunctions*
  - Chunk describes tight-binding/SGFM LDOS calculations on (8,0)/(7,1) and (8,0)/(5,3) junctions "formed with three heptagon-pentagon pairs" — an electronic-structure calculation on three-pair junctions, not a geometric construction with a single pentagon-heptagon pair, and it never states that threefold coordination is preserved.

### fi191134

> DFT–NEGF simulations of defective (6,6) single-walled carbon nanotubes show that a small population of 5-8-5 and Stone-Wales defects opens the bandgap from 0.109 eV to 0.549 eV, while higher defect concentrations reduce the bandgap back toward zero, driving the tube toward metallic behavior.

- link `992158` — *Theoretical Study of the Impact and Control of Topological Defects on the Electrical Properties of Single-Walled Carbon *
  - Chunk only sets up the study — optimizing ideal SWCNTs and captioning Figure 9's bandgap-versus-defect curves — without reporting any bandgap values or the opening/closing trend the claim asserts.

### fi191136

> Density-functional reactivity theory (DFRT) calculations on fullerene–nanotube nanobuds find that the point-group symmetry of the attached fullerene cage (D-type versus C-type) systematically shifts the kinetic, thermodynamic, and structural parameters of the sp3 junction, with D-type fullerenes for

- link `992176` — *A density functional reactivity theory (DFRT) based approach to understand the effect of symmetry of fullerenes on the k*
  - Chunk discusses the binding-energy/bond-length correlation 'within a particular symmetry type of C32 fullerene' and comparison with literature bond lengths; it never contrasts D-type against C-type symmetry, which is the substance of the claim.

### fi191155

> First-principles calculations reported a consistent picture of degraded mechanical response coupled to modified electronic structure at the nanobud junction

- link `1016968` — *Mechanical and electronic properties of carbon nanobuds: First-principles study*
  - The fragment reports one number ('the CNB with an armchair (6,6) SWCNT base was a semiconductor with a band gap of 0.71 eV') and then only announces that Young's modulus will be investigated; with no pristine comparison and no mechanical result, neither degradation nor coupling is shown.

### fi191280

> The curvature at bud sites enhances the Li binding energy

- link `994424` — *Li adsorption on a graphene–fullerene nanobud system: density functional theory approach*
  - The enhancement reported (-1.905 eV vs -1.375 eV) is for Li on the graphene side of the nanobud and is explicitly "explained by the charge distribution ... and the unit structure", so the chunk tests charge transfer, not curvature at the bud site.

### fi191283

> Density-functional calculations on carbon nanobuds find hydrogen-molecule adsorption energies ranging from 0.069 eV to 0.115 eV, an energy barrier of 2.38 eV for a hydrogen molecule to enter the C176 nanobud cage, and a maximum uptake of four H2 molecules per C176 nanobud.

- link `994410` — *Interaction between nanobuds and hydrogen molecules: A first-principles study*
  - Chunk discusses adsorption-energy trends and reports a different number ("the adsorption energy of H2 at site 5 with LDA ... is 0.39 eV"); none of the claimed values (0.069-0.115 eV, 2.38 eV, four H2) appear.

### fi191308

> Density-functional theory (DFT) calculations on nanobuds formed by creating covalent bonds between a C₆₀ fullerene and a carbon nanobowl find that only a subset of the designed junction configurations persists once the equilibrium condition is reached, that hybridization lowers the HOMO–LUMO gap bel

- link `994438` — *Design and DFT Study of New Nano Buds from the Combination of C60 Fullerene and Nanobowl*
  - The passage reports dipole moment, polarizability and hyperpolarizability trends (largest dipole/polarizability for C, highest hyperpolarizability for F) — different quantities entirely; it says nothing about configuration survival at equilibrium, the HOMO-LUMO gap, or charge transfer.

### fi191318

> The fullerene-size dependence of the bonding geometry motivates treating the PGNB's electronic character as tunable through the choice of attached fullerene.

- link `994451` — *Modulation of Dirac points and band-gaps in graphene via periodic fullerene adsorption*
  - The passage attributes tunability to a different knob — "the positions of Dirac points of graphene are predictable and controllable by changing the concentration of fullerene molecules as adsorbates" — i.e. adsorbate concentration/supercell size, not fullerene size or bonding geometry.
- link `994467` — *Modulation of Dirac points and band-gaps in graphene via periodic fullerene adsorption*
  - The passage compares cycloaddition configuration types by strain (BR sp3 angles near 109.5 degrees, BB squared rings at 90 degrees, ring-to-ring unfavorable) — a configuration-type effect, with no fullerene-size dependence and nothing about electronic tunability.

### fi191324

> A Python implementation that renders graphene and carbon-nanotube lattices in three dimensions with the PyVista library shows recurring visualization pitfalls, including bond cylinders that overlap where atoms sit too close near symmetry axes, edge atoms left without bonds because true periodic boun

- link `994444` — *3D visualization of graphene and carbon nanotubes using Python: a study.*
  - Reports a different configuration and different thresholds — slowdown 'as the number of walls and cells increased beyond 3 and 15 respectively' for MWCNTs — not the claim's 30-unit-cell threshold, and says nothing about bond overlap or missing periodic boundary conditions.

### fi192836

> Fullerenes can be subsequently converted into flexible transparent conductors and touch sensors for high-contrast displays.

- link `994462` — *57.5L:
                    <i>Late‐News Paper</i>
                    : Flexible Transparent Conductors and Touch Sensor*
  - Describes only the material and its gas-phase synthesis ('Carbon NanoBud, a hybrid of Carbon Nanotubes and fullerenes ... hybridization is achieved directly in the material synthesis process'); it neither produces nor tests transparent conductors or touch sensors.

