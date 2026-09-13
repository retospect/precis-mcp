# Ground the `ewod-oil` constraint lexicon in primary sources

`precis_chem.constraints.EWOD_OIL` (shipped ff2a4c79) carries a requirement
ledger, solvent allow/deny lists, and a 0–120 °C thermal envelope. Every
number in it traces to a **secondary literature survey** (Perplexity Sonar
deep-research, 2026-09-13), not to a corpus passage. Its docstring says so,
and no claim hub asserts any of it — deliberately.

## Why no nanopubs were minted

Checked the corpus 2026-09-13. **None of the primaries is held.** Absent:

- Pollack's EWOD thesis — the filler-fluid/solvent miscibility tables. This
  is the single load-bearing source for the whole solvent constraint
  ("silicone oil mixes with everything except water and acetonitrile";
  "FC-75 is immiscible with everything but dissolves Teflon AF").
- The Cytop/Teflon-AF composite dielectric paper — breakdown fields
  (231 V/µm for Cytop; 23–66 V/µm for Teflon AF; 68–72 V composite) and
  contact angles.
- Torabinia, organic synthesis in EWOD — the "engine-and-cargo" workaround
  for non-actuatable solvents.
- Chen et al., EWOD ¹⁸F radiolabelling chip with concentric heater/sensor
  electrodes — the 90–95% fluorination-efficiency figure.
- van Dam's EWOD radiochemistry series — 80–120 °C operating practice and
  the 78 ± 4% drying/conversion figure.

What the corpus *does* hold is `pa163983`/`pa160548` (Mugele & Baret,
"Electrowetting: from basics to applications") — a review, which
`review-source` hard-blocks as grounding — plus `pa328574` (2024, DMF PCR
thermal control) and `pa328573` (2025, "Perspectives"), which mention EWOD
only in intro prose restating the field's basics. That is testimony-grade,
the same defect already flagged on `fi337105`. Text searches for
`dielectric breakdown` + `V/µm`, `Teflon AF`, `Cytop`, and
`contact angle saturation` in an EWOD sense return zero usable passages.

So minting hubs for these numbers would produce claims that either fail
`review-source`/`primary-source` at approve, or pass admissibility while
grounded on a passage that does not own the fact. Don't.

## Work items

1. **Acquire the five primaries above** (order as listed — (1) and (2) unlock
   the two most load-bearing claims). Mint acquisition-mode findings or paper
   stubs; the fetch worker will verify or fail visibly.
2. Once (1) and (2) land, mint the solvent-immiscibility and
   breakdown-field claims properly, and **replace the `EWOD_OIL` docstring's
   "not held in the corpus" paragraph with the source handles**.
3. The lexicons themselves are survey-derived guesses at *which* terms
   matter. Re-derive the deny list from Pollack's actual miscibility table
   once held, rather than from the survey's prose summary of it.

## Prod-side work parked outside this repo

The boxel draft (`draft:nano-computer`) insert and the one genuinely
mintable artifact — a **hypothesis** that staged imine-cage boxel assembly is
executable on an EWOD-in-oil platform, with its `motivation` and
`testable_by` drafted — are prepared as exact copy-paste calls in
`~/2026-09-13-ewod-boxel-handoff.md`. They could not run: the precis MCP
disconnected mid-session. That file also holds the $0 re-import call for the
salvaged report text (`~/2026-09-13-ewod-oil-synthesis-report-partial.md`).

The remaining *code* follow-ups (structured step conditions, then
constraint-aware search rather than annotation) are in
`chem-tools-integration.md` under "Platform constraints beyond advisory" —
not duplicated here.
