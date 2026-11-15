---
status: draft
pillar: memory-graph
title: "claim conditions completeness: a claim states the conditions an expert needs to replicate it"
quest: qu635085
---

# Claim conditions completeness

Reto, 2026-11-15 (chat, on dr348633 "NO to NH3 and H2 generation"): the
thermodynamics of electrochemical steps depend on the applied potential, and
the claims often do not say which. Claims, in the hub and in the final draft
text, must be complete enough for an expert in the field to replicate them.
No pedantry: conditions the field assumes by default (about 25 °C, 1 bar,
air, standard states) stay implicit. Conditions with no default must be
stated.

This is the "scope-qualifier lint" that `aida-uri-ignores-scope.md` (Reto
2026-10-21) said needed its own spec. That ruling fixes where the conditions
go: `aida_uri` keys on the sentence alone, so a condition that changes the
fact has to be in the sentence, not only in `scope`.

## Why it happens

- The extraction prompt (`src/precis/taproot/canon.py`, `CLAIM_FORM_RULES`
  rule 3) keeps "the numbers, materials, and conditions **the passage**
  states". The passage with the number seldom restates the potential, the
  reference electrode or the electrolyte; those are in Methods or the SI.
- Scope has one optional free-text `regime` key (`_SCOPE_KEYS`, 120-char
  cap). Nothing says which conditions a given kind of claim must carry.
- No lint or gate checks for them, and the drafter copies the hub sentence
  into the bullet, so the gap reaches the text.

## Measured (read-only, prod, 2026-11-15)

All 2771 live finding hubs cited by dr348633 (4213 cited bullets). A
regex heuristic over the hub sentence plus scope sorted each hub into a
claim family by trigger words and checked the family's must-state
conditions. Hubs without a number (979) are exempt.

| Family | Quantitative hubs | Missing a must-state condition | Bullets affected |
|---|---|---|---|
| Electrochemical measurement (FE, yield rate, current, onset, overpotential) | 950 | 537 (57%) | 819 / 1475 |
| DFT step thermodynamics (ΔG, PDS, limiting potential, uphill/downhill) | 202 | 155 (77%) | 255 / 323 |
| Photocatalytic / photoelectrochemical (rate, AQY, STH) | 183 | 87 (48%) | 130 / 280 |
| Thermal catalysis | 14 | 2 | 3 / 20 |

Breakdown:

- DFT: of the 155 flagged, about 60 are adsorption-energy descriptors
  (ΔG_H*, ΔG*NO₃), where U = 0 V is the field's convention. That leaves
  about 95 step-energy, PDS and limiting-potential claims with no potential
  or reference. Only 32 of 202 state pH, 6 the solvation model and 3 the
  functional.
- Electrochemical: 122 state no potential and no current density at all;
  34 give a bare "V" with no reference electrode; 336 give no electrolyte
  or pH (the softest gap: the potential matters more than the electrolyte
  for most comparisons).
- Photo: 75 of the 87 are experimental rates with no light source; 12 are
  computed STH, which is a different family. Only 32 of 183 name the
  sacrificial agent.

Spot check of random flags: most are real gaps (e.g. fi679690 "uphill
0.34 eV ... downhill −0.17 eV" with no U; fi349380 FE at −0.6 V vs RHE but
no electrolyte; fi349795 H₂ rate with no light source). The script lives
outside the repo; step 1 below replaces it.

## Design

### 1. Conditions profiles, one per claim family

Each profile lists its trigger words, the conditions that must be stated,
the ones to state when they vary, and the field defaults that need not be
stated. Each profile is a `finding` (pattern: `conventions-as-findings.md`),
so the rule is versioned and citable and the lint cites it.

| Family | Must state | State when it varies | Default (need not be stated) |
|---|---|---|---|
| DFT step thermodynamics | applied potential and reference, or the model that fixes it ("CHE, U = 0 V vs RHE"; constant-potential) | pH; solvation (none, implicit, explicit); functional when the paper compares several | adsorption descriptors (ΔG_H*) at U = 0 V |
| DFT activation barriers | whether the step is chemical or proton-electron; for an electrochemical step, the potential | solvation | none |
| Electrochemical measurement | potential vs a named reference, or the current density / cell voltage for galvanostatic and device runs; electrolyte and concentration or pH | feed (NO partial pressure, nitrate concentration); normalisation (geometric, mass, ECSA) | room temperature, ambient pressure |
| Photocatalytic rate, AQY | light source (lamp, filter or wavelength, power) | sacrificial agent; catalyst mass; cocatalyst loading | room temperature |
| Computed STH, band edges | "computed" plus method | | |
| Thermal catalysis | temperature | gas composition, GHSV | ambient pressure |

Start with the electrochemical and DFT profiles, which this draft hits most.

Where the sets come from (Reto asked 2026-11-15). The first table is
expert judgment, not derived. Three checks turn it into evidence:

1. Published reporting standards are the source. Each profile finding
   cites the field's own protocol papers, e.g. the rigorous electrochemical
   NH₃ protocol (Andersen et al., Nature 2019), the computational hydrogen
   electrode (Nørskov et al., J. Phys. Chem. B 2004) and photocatalysis
   efficiency reporting (Qureshi and Takanabe, Chem. Mater. 2017).
   Verify these references. None was found in the corpus on 2026-11-15,
   so fetch them first.
2. Corpus variance decides what is a default. Mine the cited papers'
   Methods for each candidate condition. A condition whose value varies
   across papers, and that the result depends on, must be stated. A
   condition that is the same nearly everywhere is a default that need
   not be stated.
3. The lint's unmatched bucket finds the families we missed. Claims with a
   number that no profile covers (443 in dr348633) are reviewed for missing
   families.

Reto, or a domain expert he names, approves each profile before the lint
cites it.

### 2. A lint that flags missing conditions

A deterministic check in `src/precis/taproot/sentence_lint.py`: the
profile's trigger matches, the sentence states a number, and a must-state
condition is absent → flag `conditions-incomplete:<family>:<missing>`.
Qualitative claims are exempt. False positives are cheap; the flag only
queues work.

### 3. Mint: fill the conditions from the paper

- `CLAIM_FORM_RULES` rule 3 gains: a quantity whose value depends on a
  state variable (potential, pH, temperature, illumination) states that
  variable in the sentence.
- When the passage does not state it, the extractor reads the same paper's
  Methods or Experimental section.
- When the paper does not state it either, the claim says so ("potential
  not reported"). It never leaves the condition out silently.
- Scope gains structured condition keys (`potential`, `reference`,
  `electrolyte`, `ph`, `light`, `temperature`) next to `regime`. Scope
  feeds hub identity (`make_taproot_hub_paper_id`), so the value
  constraints of `_valid_scope_value` apply to them too.

### 4. Backfill the existing hubs

Flagged hubs go to the hub_refine queue. It already reads the evidence
paper and writes `claim_setup`. The repair writes the condition into the
sentence and scope through the claim edit door
(`nanopub-supersede-door.md`). An unsigned hub keeps its id. Each consumer
gets a `meta.claim_review_required` marker, and draft approval watermarks
are revoked. A signed or published hub refuses that edit and needs a
supersede. Chunk-addressed citations refuse sentence and scope edits until
that door can retarget them; check how dr348633's `[fi…]` cites are held
before the pilot. Run the repair on dr348633's hubs first. The drafter then
rewrites each marked bullet from the corrected hub sentence.

### 5. In the draft

- The draft's hygiene check (where the house-style check runs) flags a
  bullet that cites a `conditions-incomplete` hub.
- The drafter may state a convention shared by every claim in a section
  once, at the section's top ("Free energies use the computational
  hydrogen electrode at U = 0 V vs RHE"). The check verifies that it holds
  for every hub cited in the section; otherwise each bullet carries its
  own conditions.

## Done when

- The electrochemical and DFT profiles exist as findings, and the lint
  cites them.
- The lint runs on mint and on demand; a dry run over dr348633 gives
  counts close to the table above.
- New extractions state the potential and electrolyte when the paper does,
  covered by tests against fixture passages whose numbers have no
  conditions next to them.
- dr348633's flagged hubs are refined or superseded, and its hygiene shows
  no `conditions-incomplete` bullets other than the ones marked "not
  reported".
