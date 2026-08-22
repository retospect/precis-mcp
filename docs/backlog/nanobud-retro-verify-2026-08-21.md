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

- `fi191021` — genuinely stranded (no stamped edge at all)
- `fi191138` — genuinely stranded (no stamped edge at all)
- `fi189549` — 1 surviving edge, `draft-backfill` auto-`yes` (unverified)
- `fi191000` — 3 surviving edges, all `draft-backfill` auto-`yes`
- `fi191014` — 1 surviving edge, `draft-backfill` auto-`yes`
- `fi191167` — 1 surviving edge, `draft-backfill` auto-`yes`

**Correction (same day, on the follow-up sweep).** The original list called all six
stranded. Four of them still carry an edge the retro-verify pass never saw, because
those edges already had `support` set and so were outside the withheld extraction by
construction. Those surviving edges are `meta.origin='draft-backfill'` — written
`"support": "yes"` unconditionally at attach time, no reason, no verifier. They pass
the gate; they are not evidence anyone checked. On *verified* evidence all six hubs
are empty. → `evidence-edges-born-released.md`.

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

**Swept — the shape is rare, and that is the bad news.** Seven title patterns over
all 1714 live findings (control patterns confirmed the regex fires): 8 raw hits, 2
false positives (`fi218623`/`fi218626`, real NEGF device claims that merely say
"state-of-the-art"), leaving **5 genuine literature-about claims + 1 borderline** —
0.35% of the corpus. `fi189549` is not a systemic shape.

What it exposed instead: each of the four non-nanobud survey hubs (`fi176575`,
`fi176820`, `fi176841`, `fi177382`) has exactly one evidence edge, and every one of
them is **already stamped `support: yes`** — so a claim of the form "X has been
reviewed", cited to the review itself, is sitting past the publish gate today. Not
because a verifier was fooled: because nothing verified it. → the item below.

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


---

# Follow-up pass, 2026-08-21/22 — A and B applied

Scope decision: **nanobuds only.** The same auto-`yes` defect affects ~1208 edges
on other people's claim sets; those were deliberately left alone
(`evidence-edges-born-released.md`), as was the write-path fix.

## What was applied to prod

**A — the 30 rejected edges, deleted.** Row backup taken first. Unblocked 17 hubs
that already had verified evidence.

**B — the 44 `draft-backfill` auto-`yes` edges, pushed back.** Re-judged from their
grounding chunks by 4 opus agents under the same `_PROMPT_VERIFY` contract, with
the pre-existing `"yes"` withheld from them so it couldn't anchor the judgment.
25 `yes`, 14 `partial`, **5 rejected** (11.4% — consistent with the first pass's
12.5%), 0 contradicting. The 39 corroborating verdicts were written with
`verified_by: opus-5/autoyes-pushback`; the 5 rejects were **deleted** rather
than stripped, because stripping returns an edge to *withheld*, which blocks its
hub — and three of the five hubs had other verified evidence that would have been
blocked by a bad edge nobody stands behind.

The 5 rejects were mis-**groundings**, not false claims: `fi191263`'s chunk was the
title page and the abstract's opening lines about phosgene toxicity; `fi211523`'s
never mentions the simulation the claim rests on. The papers are right; the
attached passages were not. Re-grounding is open.

## Where the paper stands

| | hubs |
|---|---|
| publish-ready on verified or human-signed evidence | **119** |
| blocked by a live `contradicts` | 2 (`fi189542`, `fi191316`) |
| no evidence at all | 5 (`fi191014`, `fi191021`, `fi191138`, `fi211522`, `fi211523`) |
| unverified auto-`yes` remaining | **0** |

Started the day at 85 ready.

## Open — two prose/claim edits that need a handler surface

Both are agreed in substance; neither can be done with raw SQL.

**`fi191015` — prose fix in draft `173020`, chunk `2445891`.** The hub claim
("local curvature and structural defects lower the fusion reaction barrier") is
*supported* — two verified edges carry it. Earlier notes in this file called it
"a claim its own citation contradicts"; that was wrong. What the source refutes is
the **surrounding argument**: the draft cites it to rationalize *defect-driven
nucleation*, while the same paper reports that "in all considered cases the
fullerene avoids the area where the defect is located". Barrier height and spatial
preference are different things. Either re-source the nucleation framing or stop
claiming the DFT work supports it.

**`fi191021` — restate the hub claim.** The extractor narrowed a good draft
sentence. Draft: "…relate the band gap of a carbon nanobud to **both the areal
density of attached fullerenes and the chirality** of the host nanotube." Hub:
"…controlled **largely by the chirality**…" — the density half dropped, and
"largely" is a relative-importance claim no source makes. All three rejected
sources fit the draft's version. Restate the hub to the draft sentence and
re-attach them.

⚠ **Why neither is a SQL edit.** A hub carries a `finding_body` chunk (`ord 0`)
holding the claim verbatim, with its own embedding — `refs.title` alone would
leave the chunk and ANN index asserting the old claim. Draft prose is the same
story: body chunks are append-only, so a rewrite must go through
`draft_handler.edit` (DELETE+INSERT) or the embedding/summary cascade strands.
There is no `precis draft edit` CLI verb, and this repo's `precis` MCP is
read-only by project rule. Do these from the web reader, or add the verb.

**`fi191138` — re-grounding target found.** The result *is* in our copy of
"Chemical and Physical Viewpoints About the Bonding in Fullerene–Graphene Hybrid
Materials" (ref `3905`): chunk `473350` (abstract) states vdW assembly "also
governed by permanent electrostatic Coulombic interactions that contribute at
least 31%" versus FeG cycloaddition "by the formation of highly polarized chemical
bonds"; chunk `473406` carries Table 2 (EDA of G-Fullerene vs FeG-Fullerene).
`draft-backfill` attached the methods paragraph instead. Note the hub says pristine
bonding is "dominated by weak electrostatic interactions" while the paper says
vdW-assembled *with* ≥31% electrostatic — that verifies `partial`, not `yes`,
unless the hub is softened.

## Both edits applied, 2026-08-22

**`fi191021` retitled** through `edit(kind='finding', title=…)` →
`hub.refine_claim_sentence`. Old `finding_body` chunk `2612007` deleted, new
`3119389` minted with `embedding NULL` (cascade re-runs correctly). Old `pub_id`
kept as an alias, so existing `[fi191021]` cites still resolve. New claim:

> First-principles calculations relate the band gap of a carbon nanobud to both
> the areal density of attached fullerenes and the chirality of the host nanotube.

**`fi191015` prose fixed** through `edit(kind='draft', id='dc2445891')`. The
three-item citation list is intact; only the framing clause changed, plus an
in-place qualifier:

> Density-functional studies **characterize the energetics of this bonding rather
> than the siting**: local curvature and structural defects in the graphene lattice
> lower the fusion reaction barrier between C₆₀ and graphene [fi191015] **(though
> the same calculations find the fullerene avoiding the immediate vicinity of the
> defect, so barrier lowering alone does not account for preferential nucleation
> there)**, DFTB calculations show that …

### How the draft cascade actually works (the docstring is misleading)

`precis.taproot.backfill`'s docstring says a prose rewrite goes through
`draft_handler.edit` so "the chunk's DELETE+INSERT embedding/summary cascade
re-runs". It does **not** DELETE+INSERT: `dc2445891` kept its `chunk_id` and its
July embedding row. What it does is update `text` **and** `chunks.content_sha`
together, leaving `chunk_embeddings.content_sha` mismatched — and
`workers/embed.py`'s claim predicate is `NOT EXISTS (… o.status='failed' OR
o.content_sha IS NOT DISTINCT FROM c.content_sha)`, so the mismatch is exactly
what re-queues the chunk. The cascade is correct; the mechanism is sha-staleness,
not row replacement. A finding hub retitle *does* replace the row (`2612007` →
`3119389`), so the two paths genuinely differ.

⚠ The hazard the append-only rule guards against is therefore specifically a raw
`UPDATE chunks SET text = …`, which leaves `content_sha` untouched and makes the
stale embedding **permanent and invisible** — the worker will never re-claim it.

## Still open

- **`fi191021` — re-attach its 3 sources.** They were deleted in A; the claim now
  matches them. Attach withheld (`attach_evidence` takes `meta=None`; the
  `"support": "yes"` defaults live in the *callers*, not the function), then verify.
- **`fi191138` — re-ground** to ref `3905` chunk `473350` (abstract result) and/or
  `473406` (Table 2). Expect `partial` unless the hub's "dominated by weak
  electrostatic interactions" is softened to match "vdW-assembled with ≥31%
  electrostatic".
- **`fi191014` — numbers disagree with the draft.** The hub says the aerosol CVD
  distribution spans **0.7–2 nm**; the draft sentence citing it says **0.4–2 nm**;
  and the evidence pass found the source passage states *no* nanometre range at
  all (it reports TEM cage-size statistics dominated by C42/C60). Three different
  values, one of them absent from the source. Needs the real number before this
  hub gets evidence.
- **`fi211522`, `fi211523`** — no evidence; `fi211523` is a re-grounding candidate
  (its deleted edge's chunk never mentioned the simulation the claim rests on).
- **`fi189542`, `fi191316`** — blocked by a live `contradicts`; separate gate.
