---
status: draft
---

# Nanobud campaign

Grouped 2026-09-26 from 4 items that are sub-parts of one deliverable (each keeps its own section below; the originals are in the history). Split a section back out only when it becomes independently shippable.

## Nanobud paper (dr173020) — batch-3 open queue

_Grouped 2026-09-26; was `nanobud-nanopub-batch3`, status draft._

Scope rule (Reto): **true nanobuds only** — covalently bonded
fullerene–tube/graphene hybrids; no vdW/deposited/mixed systems in the
claim tree. Cited-finding count and re-approval state for this cohort are
owned by `claim-review-mechanism.md`'s worked-example section — point
there rather than restate (all `nanopub_publish` rows, including
fi191146, reset to `candidate` by the title/body reconciliation pass; the
sign-off queue below is a full re-review, not just the withheld edges).

### Sign-off queue

Withheld-edge sign-offs + re-sign for the full reviewed cohort (state
reset — see above). Nuance to eyeball at sign: fi211520/21 quotes say
"for bulk graphite" (bulk-equivalent units) while their titles say
monolayer graphene. Open keep-or-drop calls surfaced during batch review,
still Reto's: fi191307/fi191308 (near-duplicate hubs, both ground pa5887 —
design vs electronic-results); fi191323 (methodological-capability claim —
asserts about method, not nature); fi189526 (literature-scope claim —
about the literature, not nature).

### Seven blocked adjudications (Reto's call, each)

- **fi191169** — OEM-supply claim unsupported: paper says "prototyping
  phase with >30 customers," not OEM supply; no epistemic method.
- **fi191282** — source ref 3838 (Terzyk) has no DOI row; needs DOI
  backfill before a passage is mintable.
- **fi191286** — only grounding chunk is hearsay citing Mpourmpakis;
  needs the primary imported or the claim stays unminted.
- **fi191316** — live `contradicts` edge from fi192706 (claim-strength
  inflation: "will ultimately require" vs source's "could be used");
  adjudicate before approve.
- **fi189536** — evidence chunk is an SCC-DFTB adsorption study that
  never asserts bilayer-deposition/blending; only secondary review pa4801
  says so, secondhand.
- **fi189542** — sole chunk is an intro recap with a `[15]` marker + a
  live `contradicts` edge from ref 5828.
- **fi189549** — bibliographic meta-claim about a micro-review; no
  epistemic mode.

### Staged n1–n9 go/no-go

9 non-covalent own-result hubs staged outside the nanobud claim tree
(covalency-rejected papers), awaiting Reto's explicit go/no-go.
Scratchpad session `e26f279b`, `specs3/`: `mint-*.json` + `approve-*.json`
(hub=MINT-PENDING) + `submit3.sh` paced runner. Contents: pa39796 — n1
optimum 25±8% filling / n2 50% collapse / n3 GCMC LJ vdW-stacked; pa40723
— n4 Li energies −1.917…−2.642 eV / n5 no-clustering −1.863 vs −1.030 eV /
n6 metallic-character; pa170590 — n7 dihedral switch / n8 HOMO–LUMO / n9
α-β-μ. All titles carry explicit non-covalent/adsorptive/fullerene–
fullerene framing; pa40723 + pa170590 are grounded in own-abstract text
(first-page-only ingest — non-hearsay, but shallow; see re-ingest list).

### Un-cited weave-in: fi211848–54 / h1–h5

Minted and approved (`reviewed`) but cited nowhere in dr173020: fi211848–
54 (Tier-2 — pa4365, pa948, pa206485, pa1797) and h1–h5 (`specs4` —
pa4365, pa948). Need weaving into the electronic-structure/
characterization sections, including the pa948-vs-pa4365 band-gap
disagreement. Reto may retire fi211852/53 (pa206485 passed covalency
review only WITH caveat — spray-coated fullerenol clusters,
linker-mediated covalency cited to a secondary ref, "nanobud" appears
nowhere in-paper). (H1's original grounding hit the `<sup>N</sup>`
citation-marker-residue gate hole, fixed in `5d1ef498`.)

### dc-level scope collisions and suspects

Found pre-scope-rule, dc2445932 (energy-storage ¶):

- Koh et al. cited as "Li adsorption on graphene–fullerene nanobuds"
  [fi191280], but pa40723's own system is adsorptive C60-on-SWCNT
  (non-covalent; covalency is hearsay).
- Hernández Mendoza clause [fi191286] — "reinforcing the convergent
  picture that bud-site curvature favours gas adsorption across
  independent computational studies" overclaims: pa1638 is experimental
  with zero adsorption data (H2 deferred to future work); supportable
  only as synthesis route + FTIR bonding.
- Terzyk fullerene-intercalated nano-containers [fi191282] is non-covalent
  intercalation inside the nanobud narrative.

Suspected, not fixed (Reto review): dc2445908 uncited "soluble
derivatives" first sentence + downstream solubility thread; dc2445887
induction-plasma specifics have no corpus source; dc2445902 5-vs-6
heptagon distinction could be stated; dc2445888 −0.48 eV end unverified;
dc2445939 says B3LYP-D3 but pa3941 used CAM-B3LYP.

**pc436382 on dc2445855 — Reto's call**: confirmed wrong source
(biomass-catalysis review, secondhand Nobel mention). Drop the handle
(paragraph already cites Kroto + Novoselov/Geim) or name the intended
bibliography entry.

### Re-ingest list

pa199068, pa40723, pa170590 — abstract+intro-only ingests, results not in
store; re-ingest before mining either harder. pa1797's chunk pc175737 is
OCR-corrupted beyond grounding (separate defect, not an ingest depth
issue). pa1638 stays genuinely DOI-less (Sway-distributed congress paper,
`cite_key` tecnia22) — its passage (h11, mint-only, `candidate`) stays out
of the payload under the one-DOI rule; edge visible internally only.

### Mining queue

The ten ranked tier-2 mint candidates from the 2026-08-17 survey — each with
its grounding chunk — plus the marginals and the draft-integrity flags now
live in `nanobud-claim-mining-candidates.md`. None are minted.

## Do the passages actually say what the claims say?

_Grouped 2026-09-26; was `nanobud-grounding-audit-2026-08-20`, status draft._

Run 2026-08-20 over every claim hub cited by the nanobuds draft (`dr173020`),
four read-only agents, one quarter each. Each hub's sentence was read against the
full text of every grounding passage on its evidence edges. This is the check no
gate performs: mint verifies that an edge exists and that the source is primary,
never that the passage supports the sentence.

It was prompted by two HKUST-1 hubs (`fi176432`, `fi177486`) found grounded to a
hardness passage and to a ZIF-8 paper respectively — both with valid-looking
edges. Those are in `dr42995`'s cohort, not this one; **the boxel document has
not been audited and should be next.**

### Result

| verdict | count |
|---|---|
| SUPPORTED | ~106 |
| PARTIAL | ~10 |
| NO_GROUNDING | 7 |
| CONTRADICTED | 2 → **0 on verification** |
| UNSUPPORTED | 0 |
| JOINTLY_ONLY | 0 |

Counts are approximate by 2–3: the quarters overlapped at their boundaries
(191155/191156 and 191292 were each audited twice) and one batch's own summary
disagreed with its detail table on a ref id. Re-derive before quoting. Zero
`JOINTLY_ONLY` is worth noting — the feared "supported only in conjunction"
case did not appear at all.

### The two contradictions — fix before anything else

**Both verdicts were checked against the source papers and both are wrong. There
are zero contradictions in this cohort.** Each failed in a different direction,
and acting on either would have edited a correct claim into an incorrect one.

- **`fi192819`** (the audit said `fi191323`; that ref is an unrelated finding
  about computational approaches — the id was wrong, as the caveat above
  warned). Claims field-emission threshold fields **1–2 V/μm**; its grounding
  chunk in `pa494` reads *"in the range of 1–2 V/ mm"*. The audit called the
  claim wrong. **It is backwards — the claim is right and the passage is
  corrupt.** `pa494` contains **zero** micro signs (U+03BC/U+00B5) and **zero**
  Greek letters anywhere in its chunks, while `°±×–—Å` survive intact — a
  Symbol-font extraction failure that drops the Greek range and nothing else.
  The paper states the unit in prose in another chunk: *"threshold field values
  of few Volts per micron."* Corpus-wide, `V/μm` appears 82 times against 25
  stripped forms. 1–2 V/μm is also the ordinary value for CNT/nanographene
  emitters; 1–2 V/mm would beat every emitter ever reported by a thousandfold.
  **Do not edit this claim.** The defect is in ingest —
  `ingest-strips-greek-glyphs.md`.
- **`fi211523`** — claims the laddering effect has been shown "only in molecular
  dynamics simulations, not on physically fabricated junctions". The audit read
  *"we present a novel method to produce"* and *"is exhibited in this work"* as
  experimental. `pa2857` is **pure simulation**: LAMMPS, Tersoff–Brenner and
  Lennard–Jones potentials, Nosé–Hoover thermostat, 0.5 fs timestep, strain
  rates of 10⁸ s⁻¹. Its every experimental keyword is a citation to other
  groups' work, and its conclusion says *"how to form individual embedding
  nanobuds junction experimentally is still quite challengeable… further
  theoretical and experimental works for the validation of the proposed
  mechanism will be planned in the future."* The claim is exactly right.
  **Do not edit this claim.**

### What this does to the rest of the audit

Two verdicts were checked; two were wrong. These were the *highest-confidence*
findings — the ones where an LLM reader saw a flat numeric or logical
contradiction — so the PARTIALs below, which rest on subtler judgments, carry at
least the same error rate. **Nothing in this file may be acted on without
reading the source paper first.** Re-verify before each edit; do not batch.

A passage-versus-sentence mismatch has **three** causes and the audit's taxonomy
admitted only one:

1. the claim is wrong;
2. the **passage** is corrupt (extraction scar — `fi192819`);
3. the reader **misjudged what kind of study** the source is (`fi211523`).

Cause 2 is the dangerous one, because it inverts the fix: the corpus looks like
it contains a wrong claim when it actually contains a wrong *source text*, and
"correcting" the claim propagates the scar into a signed artifact. Any future
version of this pass must check the source paper's character inventory and study
type before recording a contradiction.

### Seven hubs cannot be audited at all

`fi191292`–`fi191296` carry **no evidence edges whatsoever** despite being cited
by the draft. `fi190978` and `fi191002` have ref-level edges with
`src_chunk_id IS NULL`, so there is no passage to read. Either way the draft
cites claims whose support cannot be inspected.

### The failure class worth naming

The PARTIALs are not bad citations. They are claims asserting **more structure
than the passage supports** — a cause, a comparison, or a generality the source
never states:

- `fi191280` — attributes enhanced Li binding to bud-site **curvature**; sources
  show the enhancement but attribute it to charge transfer and electrostatics.
- `fi191286` — curvature-favours-adsorption is demonstrated for pristine CNTs,
  generalised in the claim to nanobud bud sites.
- `fi191170` — "improved sensitivity" with no baseline in the source; absolute
  performance is measured, the comparison is not.
- `fi191148` — claim is about *pored* graphene; passages are about regular graphene.
- `fi191283` — barrier and capacity check out; the adsorption-energy range
  0.069–0.115 eV appears in no passage.
- `fi191014` (0.7–2 nm vs 0.4–2 nm) and `fi191281` (330 cycles vs 300) are
  numeric drift of the same family.

`fi191280` and `fi191286` independently reproduce what an earlier session flagged
as dc-level scope collisions — two passes reading, not pattern-matching.

This class is invisible to every existing gate: the edge is real, the source is
primary, the quote verifies. Only reading the passage against the sentence finds
it. That argues for making this audit a pass rather than a one-off — see the
`hub_refine` verification machinery (`_verify_support_with_caveats` already
returns exactly this judgment for *candidate* evidence; nothing re-checks
*attached* evidence).

### Caveat on the method

Verdicts are LLM judgments over passage text, advisory and unreviewed. They are
detection, not truth: no write was made and none should be made from this file
without a human reading the passage. The value is that it narrows 126 hubs to
about a dozen worth a person's attention.

## The edges that were never verified at all

_Grouped 2026-09-26; was `nanobud-retro-verify-2026-08-21`, status draft._

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

### Result

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

### The apply is asymmetric, deliberately

`preflight.withheld_edges` gates on `meta->>'support' IS NULL`, so writing **any**
support value releases the edge. A rejection must therefore NOT be written back as
`support:"no"` — that would publish exactly what the pass just rejected. Rejected
edges were left untouched and are listed below. Anyone running this pass on another
claim set must know this before writing anything.

Undo for the 209: `meta - 'support' - 'caveats' - 'support_reason' - 'verified_by'
- 'verified_at'` over the applied link_ids (recoverable from
`meta->>'verified_by' = 'opus-5/retro-verify'`).

### Needs a human

#### Stranded hubs — every evidence edge rejected

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

#### A claim its own citation contradicts

`fi191015` asserts:

> Local curvature and structural defects in the graphene lattice lower the fusion reaction barrier between C₆₀ and graphene.

Its cited source (*Impact of Local Curvature and Structural Defects on Graphene–C60 Fullerene Fusion Reaction Barriers*) reports the opposite — Runs counter to the claim: after computing potential energy surfaces for the defect-containing SLG models, "the calculations shows that in all considered cases the fullerene avoids the area where the defect is located", i.e. defects repel rather than facilitate the fusion.

This is a claim-authoring fix, not an edge removal.

#### A claim shape that can never verify

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

#### Conjunctive claims (not blocking, but decide before signing)

157 of 239 verdicts were scoped `partial`, and the
reason was consistent across batches: the hub claims are *conjunctions* and no
single paper covers the whole conjunction. Only 40 of 108 hubs have even one clean
`yes`; 62 have nothing but scoped partials. They pass the gate (`is_corroborating`
admits a non-contradicting partial) and will publish with every citation caveated.
That is a claim-shape problem the taproot extractor created at authoring time.

### The 30 rejected edges

#### fi189549 — **STRANDED**

> The synthesis, properties, structural peculiarities, and applications of nanobuds and closely related hybrid nanostructures have been surveyed, combining experimental observations with density-functional-theory predictions.

- link `992062` — *The local study of a nanoBud structure*
  - The chunk is the conclusion of a single experimental TEM/STM/Raman study of one NanoBud with (16,11) chirality; it is not a survey of synthesis, properties and applications and contains no density-functional-theory predictions.
- link `992063` — *Understanding the Interaction Between Fullerene and Graphene Nanoribbons Using Density Functional Theory*
  - The chunk reports one DFT study of B- and N-doped C60-ZGNR nanobud stability and bond lengths; it neither surveys the field nor combines experimental observations with the calculations.
- link `992064` — *Interaction between nanobuds and hydrogen molecules: A first-principles study*
  - The chunk describes a first-principles H2 adsorption-energy calculation on nanobuds - a single focused computational study, not a survey of synthesis, properties, structural peculiarities and applications.

#### fi191000 — **STRANDED**

> Spin-polarized DFT-D3 calculations including pairwise van der Waals dispersion find carbon nanobud formation on carbon nanotube sidewalls endothermic, with C60 binding energies of +0.736 eV on the (10,0) tube and +0.685 eV on the (5,5) tube, a sidewall formation barrier of 1.92 eV and dissociation b

- link `992119` — *Supplementary Information*
  - The table lists cap bonding geometries with binding energies of -0.276, 0.210, -0.200, 0.514 ... 2.843 eV; none of the claimed values (+0.736/+0.685 eV sidewall binding, 1.92/0.77 eV sidewall barriers, 1.52/1.46 eV cap barriers) appear, and the chunk reports no barriers, no (10,0)/(5,5) results, and no DFT-D3 or spin-polarization details.

#### fi191014 — **STRANDED**

> The original aerosol CVD process yields a broad distribution spanning 0.7–2 nm

- link `994387` — *Investigations of NanoBud formation*
  - The chunk reports fullerene diameters "in the size ranging from 0.4 to 2 nm" measured when 145 ppm H2O vapor was introduced in the reactor, not the 0.7-2 nm distribution of the original process that the claim names.

#### fi191021 — **STRANDED**

> First-principles calculations converge on a picture in which the band gap of a carbon nanobud is controlled largely by the chirality of the host nanotube.

- link `994390` — *First-principles study of the structural, energetic and electronic properties of C<sub>20</sub>-carbon nanobuds*
  - The chunk attributes gap control to a different variable than the claim: "the band gaps can be tuned by controlling the density of adsorbed C20", with no comparison across host-nanotube chiralities.
- link `994393` — *Electronic and magnetic properties of small fullerene carbon nanobuds: A DFT study*
  - The table lists Egap only for Cn-(5,5) nanobuds, varying fullerene size and cycloaddition configuration at a single host chirality, so it cannot show that chirality is the controlling factor.
- link `994394` — *Electronic and magnetic properties of small fullerene carbon nanobuds: A DFT study*
  - The table lists Egap only for Cn-(5,0) nanobuds, and within that single chirality the gaps still vary widely with fullerene size and configuration (0.07-0.32 eV); no chirality comparison is made in this passage.

#### fi191138 — **STRANDED**

> Dispersion-corrected DFT (PBE-D3) calculations with AIM and energy-decomposition bonding analysis find that fullerene bonding on pristine graphene is dominated by weak electrostatic interactions, whereas adsorption onto Fe-doped graphene forms markedly stronger covalent bonds.

- link `992148` — *Chemical and Physical Viewpoints About the Bonding in Fullerene–Graphene Hybrid Materials: Interaction on Pristine and F*
  - Only sets up the analysis ('The chemical perspective ... was studied by the AIM and NBO schemes') and captions Figure 3; it reports no electrostatic-versus-covalent result for pristine versus Fe-doped graphene.
- link `992153` — *Chemical and Physical Viewpoints About the Bonding in Fullerene–Graphene Hybrid Materials: Interaction on Pristine and F*
  - Describes the ALMO-EDA decomposition terms and points to Table 2/Figure 4 but reports no values or comparison, so it does not establish weak electrostatic bonding on pristine versus stronger covalent bonding on Fe-doped graphene.

#### fi191167 — **STRANDED**

> B3LYP and dispersion-corrected B3LYP-D3 calculations on nanobuds designed from C20 and C60 fullerenes find HOMO-LUMO gaps of 1.7 eV and 2.14 eV for structures A and B, narrowing to 0.76 eV and 1.11 eV under the D3 correction, below the 4.41 eV and 1.92 eV of pristine C60 and the 3.74 eV and 2.74 eV 

- link `994401` — *Design and computational study of the novel nano-buds of C20@C60 with high NLO properties*
  - The chunk is entirely about electric charge transfer from C60 to C20 and the resulting dipole moment in structures A and B; it reports none of the claimed HOMO-LUMO gaps (1.7, 2.14, 0.76, 1.11, 4.41, 1.92, 3.74, 2.74 eV) and no B3LYP versus B3LYP-D3 comparison.

#### fi189524

> Hybridization of fullerenes with two-dimensional nanomaterials improves the physical and chemical properties of the 2D host

- link `993366` — *Design and analysis of sandwiched fullerene-graphene composites using molecular dynamics simulations*
  - The passage only establishes thermodynamic stability of the atomistic models and prospective manufacturability ("manufacturing of the proposed FG material systems looks like attainable in the future"); it reports no physical or chemical property of the 2D host being improved.

#### fi189548

> Spin-polarized density-functional calculations predict that all-carbon nanobuds carry a substantial density of unpaired spins from carbon radicals created by geometry-induced electronic frustration in the region connecting the fullerene to the nanotube surface, giving net magnetic moments of 6.0 μB 

- link `993372` — *Magnetic properties of all-carbon graphene-fullerene nanobuds*
  - This chunk concerns graphene-C54 nanobuds and only tabulates NM/AFM/FM energy differences and a binding energy - a different system with no unpaired-spin-density mechanism and none of the claimed 6.0/4.25 mu_B moments.

#### fi190976

> Continuous aerosol (floating-catalyst) chemical-vapour-deposition synthesis from ferrocene vapour decomposition in a carbon-monoxide atmosphere demonstrates the formation of carbon nanobuds in a single continuous reactor, with the maximum fullerene coverage of the nanotube surfaces reached at a reac

- link `992128` — *A novel hybrid carbon material*
  - The chunk is an STM electronic-states argument for "the covalent nature of the fullerene-SWNT bond"; it says nothing about aerosol CVD synthesis, ferrocene/CO, a continuous reactor, or a temperature optimum.
- link `993378` — *Investigations of NanoBud formation*
  - The chunk only reports that in situ sampling was done 'at the set temperature of 1000 C, corresponding to the maximum reactor temperature of 1058 C' with 125 ppm H2O; it says nothing about ferrocene, a CO atmosphere, nanobud formation, or fullerene coverage peaking at 1000 C.

#### fi190978

> Fullerene nucleation is favoured at defect sites on the CNT surface

- link `992112` — *A novel hybrid carbon material*
  - The chunk reports DFT energetics of fullerenes ester-bonded to single-vacancy SWNTs and states those configurations 'are metastable with respect to forming perfect tubes together with oxidized fullerenes' — it addresses post-formation stability, not whether nucleation is favoured at defect sites, and involves no in situ sampling.

#### fi191009

> Wet-chemical 1,3-dipolar cycloaddition (the Prato reaction) on a graphene–C₆₀ nanobud hybrid demonstrates that organic functionalisation occurs selectively on the fullerene cages while the graphene sheet itself stays unfunctionalised, and that the attached hydrophilic groups markedly increase the di

- link `993379` — *Graphene nanobuds: Synthesis and selective organic derivatisation*
  - This is a bare synthesis recipe (20 mg pG-C60 with 3,4-dihydroxybenzaldehyde and sarcosine in DMF, heated 3 h, filtered, re-dispersed in water); it neither demonstrates selectivity toward the fullerene cages nor reports any dispersibility result.

#### fi191011

> Potentiostatic electrochemical treatment of graphene films in an aqueous 0.5 mol L⁻¹ H₂SO₄ electrolyte demonstrates in situ growth of carbon nanobuds on the graphene sheet surface between 1.4 V and 2.0 V, the buds growing larger and forming carbon nanoballs as the applied constant potential increase

- link `992143` — *<i>In situ</i> growth of novel carbon nanobuds and nanoballs on graphene nanosheets by the electrochemical method*
  - Chunk only describes the Raman D and G peaks near 1350 and 1580 cm-1 and the general utility of Raman for graphene; it makes no statement about nanobud growth, potential dependence, or nanoball formation.

#### fi191015

> Local curvature and structural defects in the graphene lattice lower the fusion reaction barrier between C₆₀ and graphene.

- link `992140` **[CONTRADICTS]** — *Impact of Local Curvature and Structural Defects on Graphene–C60 Fullerene Fusion Reaction Barriers*
  - Runs counter to the claim: after computing potential energy surfaces for the defect-containing SLG models, "the calculations shows that in all considered cases the fullerene avoids the area where the defect is located", i.e. defects repel rather than facilitate the fusion.

#### fi191132

> Geometric construction shows that inserting a single pentagon–heptagon defect pair into an otherwise perfect hexagonal carbon-nanotube lattice preserves the threefold coordination of every carbon atom while accommodating a change in tube radius or chiral indices across the junction.

- link `994392` — *Pure Carbon Nanoscale Devices: Nanotube Heterojunctions*
  - Chunk describes tight-binding/SGFM LDOS calculations on (8,0)/(7,1) and (8,0)/(5,3) junctions "formed with three heptagon-pentagon pairs" — an electronic-structure calculation on three-pair junctions, not a geometric construction with a single pentagon-heptagon pair, and it never states that threefold coordination is preserved.

#### fi191134

> DFT–NEGF simulations of defective (6,6) single-walled carbon nanotubes show that a small population of 5-8-5 and Stone-Wales defects opens the bandgap from 0.109 eV to 0.549 eV, while higher defect concentrations reduce the bandgap back toward zero, driving the tube toward metallic behavior.

- link `992158` — *Theoretical Study of the Impact and Control of Topological Defects on the Electrical Properties of Single-Walled Carbon *
  - Chunk only sets up the study — optimizing ideal SWCNTs and captioning Figure 9's bandgap-versus-defect curves — without reporting any bandgap values or the opening/closing trend the claim asserts.

#### fi191136

> Density-functional reactivity theory (DFRT) calculations on fullerene–nanotube nanobuds find that the point-group symmetry of the attached fullerene cage (D-type versus C-type) systematically shifts the kinetic, thermodynamic, and structural parameters of the sp3 junction, with D-type fullerenes for

- link `992176` — *A density functional reactivity theory (DFRT) based approach to understand the effect of symmetry of fullerenes on the k*
  - Chunk discusses the binding-energy/bond-length correlation 'within a particular symmetry type of C32 fullerene' and comparison with literature bond lengths; it never contrasts D-type against C-type symmetry, which is the substance of the claim.

#### fi191155

> First-principles calculations reported a consistent picture of degraded mechanical response coupled to modified electronic structure at the nanobud junction

- link `1016968` — *Mechanical and electronic properties of carbon nanobuds: First-principles study*
  - The fragment reports one number ('the CNB with an armchair (6,6) SWCNT base was a semiconductor with a band gap of 0.71 eV') and then only announces that Young's modulus will be investigated; with no pristine comparison and no mechanical result, neither degradation nor coupling is shown.

#### fi191280

> The curvature at bud sites enhances the Li binding energy

- link `994424` — *Li adsorption on a graphene–fullerene nanobud system: density functional theory approach*
  - The enhancement reported (-1.905 eV vs -1.375 eV) is for Li on the graphene side of the nanobud and is explicitly "explained by the charge distribution ... and the unit structure", so the chunk tests charge transfer, not curvature at the bud site.

#### fi191283

> Density-functional calculations on carbon nanobuds find hydrogen-molecule adsorption energies ranging from 0.069 eV to 0.115 eV, an energy barrier of 2.38 eV for a hydrogen molecule to enter the C176 nanobud cage, and a maximum uptake of four H2 molecules per C176 nanobud.

- link `994410` — *Interaction between nanobuds and hydrogen molecules: A first-principles study*
  - Chunk discusses adsorption-energy trends and reports a different number ("the adsorption energy of H2 at site 5 with LDA ... is 0.39 eV"); none of the claimed values (0.069-0.115 eV, 2.38 eV, four H2) appear.

#### fi191308

> Density-functional theory (DFT) calculations on nanobuds formed by creating covalent bonds between a C₆₀ fullerene and a carbon nanobowl find that only a subset of the designed junction configurations persists once the equilibrium condition is reached, that hybridization lowers the HOMO–LUMO gap bel

- link `994438` — *Design and DFT Study of New Nano Buds from the Combination of C60 Fullerene and Nanobowl*
  - The passage reports dipole moment, polarizability and hyperpolarizability trends (largest dipole/polarizability for C, highest hyperpolarizability for F) — different quantities entirely; it says nothing about configuration survival at equilibrium, the HOMO-LUMO gap, or charge transfer.

#### fi191318

> The fullerene-size dependence of the bonding geometry motivates treating the PGNB's electronic character as tunable through the choice of attached fullerene.

- link `994451` — *Modulation of Dirac points and band-gaps in graphene via periodic fullerene adsorption*
  - The passage attributes tunability to a different knob — "the positions of Dirac points of graphene are predictable and controllable by changing the concentration of fullerene molecules as adsorbates" — i.e. adsorbate concentration/supercell size, not fullerene size or bonding geometry.
- link `994467` — *Modulation of Dirac points and band-gaps in graphene via periodic fullerene adsorption*
  - The passage compares cycloaddition configuration types by strain (BR sp3 angles near 109.5 degrees, BB squared rings at 90 degrees, ring-to-ring unfavorable) — a configuration-type effect, with no fullerene-size dependence and nothing about electronic tunability.

#### fi191324

> A Python implementation that renders graphene and carbon-nanotube lattices in three dimensions with the PyVista library shows recurring visualization pitfalls, including bond cylinders that overlap where atoms sit too close near symmetry axes, edge atoms left without bonds because true periodic boun

- link `994444` — *3D visualization of graphene and carbon nanotubes using Python: a study.*
  - Reports a different configuration and different thresholds — slowdown 'as the number of walls and cells increased beyond 3 and 15 respectively' for MWCNTs — not the claim's 30-unit-cell threshold, and says nothing about bond overlap or missing periodic boundary conditions.

#### fi192836

> Fullerenes can be subsequently converted into flexible transparent conductors and touch sensors for high-contrast displays.

- link `994462` — *57.5L:
                    <i>Late‐News Paper</i>
                    : Flexible Transparent Conductors and Touch Sensor*
  - Describes only the material and its gas-phase synthesis ('Carbon NanoBud, a hybrid of Carbon Nanotubes and fullerenes ... hybridization is achieved directly in the material synthesis process'); it neither produces nor tests transparent conductors or touch sensors.


---

## Follow-up pass, 2026-08-21/22 — A and B applied

Scope decision: **nanobuds only.** The same auto-`yes` defect affects ~1208 edges
on other people's claim sets; those were deliberately left alone
(`evidence-edges-born-released.md`), as was the write-path fix.

### What was applied to prod

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

### Where the paper stands

| | hubs |
|---|---|
| publish-ready on verified or human-signed evidence | **119** |
| blocked by a live `contradicts` | 2 (`fi189542`, `fi191316`) |
| no evidence at all | 5 (`fi191014`, `fi191021`, `fi191138`, `fi211522`, `fi211523`) |
| unverified auto-`yes` remaining | **0** |

Started the day at 85 ready.

### Open — two prose/claim edits that need a handler surface

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

### Both edits applied, 2026-08-22

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

#### How the draft cascade actually works (the docstring is misleading)

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

### What the judge calibration says about this pass (2026-08-25)

`llm-judge-reliability.md` measured the grounding judge against a
30-edge human gold set. Two results bear directly on the numbers above,
and they point the *opposite* way from where attention has gone:

- **Rejections are the trustworthy verdict.** Across 30 adjudicated
  edges the judges produced **zero false alarms** — every flagged defect
  was a real defect. So the 30 rejects here, and the 5 from the
  auto-`yes` pushback, are very likely all genuine. Deleting them was
  right and does not need re-litigating.
- **Releases are the untrustworthy verdict.** 3 of 11 judge-`NONE`
  verdicts (27%) hid a real defect on that sample. The analog here is
  the **209 released edges** (52 `yes` + 157 scoped `partial`): they are
  exactly the "judge said it's fine" bucket, and nothing has sampled
  them. That sample was disagreement-enriched so 27% is an upper bound,
  but even a third of it over 209 edges is ~20 bad releases sitting past
  the publish gate.

**Therefore, before signing:** draw a random sample of the 209 released
edges (~25–30) and adjudicate them the way the gold set was built. That
is the highest-value remaining check on this paper's soundness — higher
than re-examining the rejects, and higher than the individual repairs
below, because it is the only bucket where a silent error survives to
publication. The frozen instrument
(`docs/runbooks/grounding-verifier-instrument.md`) and the scoring script
(`llm-judge-reliability-data/score_vs_gold.py`) both apply unchanged.

Also inherited from that item: **do not add majority voting** to a
re-check pass — on the gold set a single judge beat the 3-way modal
(87% vs 80%), because consensus suppressed correct minority findings.

### Still open

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

## Ten mintable candidates, already grounded

_Grouped 2026-09-26; was `nanobud-claim-mining-candidates`, status draft._

Surveyed 2026-08-17 by a read-only opus agent over the tier-2 nanobud papers;
rehomed here 2026-08-20 from `nanobud-nanopub-batch3.md` when that file was
compacted to its open decisions. Nothing here has been minted. Each entry
names the paper, the claim, and the grounding chunk, so each is a
`precis taproot mint` away once the sentence is written to canon.

This is the **inward** loop's remaining queue — claims a human found by
reading. It is complementary to evidence widening (`taproot-reground.md`),
which walks the corpus outward from hubs that already exist.

| # | paper | claim | grounding |
|---|---|---|---|
| 1 | pa4365 | configuration-selected metallicity: 0.12 eV type-II vs metallic I/III vs 0.18 eV embedded | pc550457 (alt pc550458) |
| 2 | pa4365 | 3.0 Å spacing threshold + mirror-symmetry-breaking mechanism | pc550459, citation-free |
| 3 | pa948 | ¹³C CSI/CSA localized at the attachment site — the paper's headline novelty, and an NMR handle | pc82612 (tighter alt pc82603) |
| 4 | pa948 | band gaps 0.57–0.76 eV vs pristine 1.82/0.81 eV | pc82597 |
| 5 | pa206485 | rise/fall 33.53/0.934 ms → 86.42/3.35 ms at Vgs −21 V — the missing speed axis | pc2901217 |
| 6 | pa39796 | fullerene Raman **suppressed** in the stacked composite | pc1260158 (non-covalent framing) |
| 7 | pa1797 | write actuation 0.457 THz, 1.6–1.65 V/nm, 5 ps | pc175726 / pc175740 ("free C₆₀" framing) |
| 8 | pa948 | charge transfer 0.032 → 0.72 C | pc82598 |
| 9 | pa206485 | detectivity 2.34e10 Jones | abstract pc2901189 only — the body has just the formula |
| 10 | pa39796 | pillared gallery height 2.3–3.4 nm | pc1260163 (non-covalent framing) |

**Candidate 4 is worth minting first.** It puts a live *disagreement* with
pa4365's 0.12/0.18 eV on the record — finite H-capped vs periodic models —
and the corpus currently holds zero hub↔hub disagreements of any kind. It is
the cheapest real test of the opposition machinery on genuine physics rather
than a synthetic case.

Candidates 6, 7 and 10 carry **non-covalent or "free C₆₀" framing**. Per the
standing scope rule (true nanobuds only; the boundary is all-carbon, not
covalency) each needs its scope stated explicitly at mint or it will read as
a nanobud claim it is not.

### Marginals — on file, not recommended

pa4365 (5,5)-host gaps pc550465 · pa948 bond lengths pc82599 · pa206485
532 nm selectivity pc2901206 · pa1797 K@C60 −0.96e endohedral pc175732 ·
pa1797 temperature-insensitive 9 ps switching pc175729/30 · pa39796 pore-size
shift pc1260167 · pa40723 anti-clustering −1.863 vs −1.030 eV pc1307817
(overlap risk with the own-work agent) · pa170590 functional benchmark
pc2409795 (skip).

### Draft-integrity flags — Reto's calls, unresolved

- **pa1638 reports NO hydrogen-storage measurement despite its title**
  (conclusion pc155443 is "further work"). `dr173020` must not cite it as
  H₂-storage evidence — only as synthesis route + FTIR bonding.
- **pa199068 / pa40723 / pa170590 are abstract+intro-only ingests.** Results
  are not in the store; re-ingest before mining them harder.
- **pa170590 calls a C20–C40 dimer a "nanobud"** — useful for the review's
  scope-definition section precisely because it is the boundary case.
- **pc175737 (pa1797) is OCR-corrupted beyond grounding.**
- **pa206485's stability sentence straddles a chunk boundary** — unmintable as
  it stands.

### One durable schema note

`chunks` and `refs` both soft-delete via **`retired_at`** (unified by
vocab-compaction Stage E — `refs` used to spell it `deleted_at`, a frequent
source of ad-hoc-query bugs; that quirk is gone). Ad-hoc chunk/ref queries
that omit a `retired_at IS NULL` filter still silently include retired rows.
