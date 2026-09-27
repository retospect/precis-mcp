---
status: draft
title: norr-her-meta — quantitative meta paper over the norr-her-survey corpus
prio: normal
---

# norr-her-meta — quantitative meta paper over the norr-her-survey corpus

Campaign item, decided with Reto 2026-09-27 (worktree paper-extraction). It
names two things so they stop being confused:

- **`norr-her-meta`** — the campaign and its eventual draft: a quantitative
  meta-analysis of NO/nitrite/nitrate-to-NH3 and HER on solid catalysts, built
  on the claim hubs of the `norr-her-survey` draft (dr348633, 1664 hubs tagged
  `campaign:norr-her-survey`). Closed measurand list, one or two cells of the
  survey's mode × side × catalyst partition, a handful of figures with an
  accuracy number behind them.
- **`quantbind`** — the extraction machinery: the source-bound binding
  encoding of `paper-extraction-pilot/` (anchor ids instead of quotations,
  shared condition defaults, table recipes, `ANCHOR_SCHEME` guard). Items 1–8
  of `corpus-quantitative-extraction.md` §"Source-bound quantitative
  extraction" are quantbind fixes; this item consumes quantbind as is.

## Motivation / why

The survey answers "what do the papers say". The integrated 50-page review
Reto described 2026-09-20 (`corpus-quantitative-extraction.md` §"Descriptor
graph") needs the experimental↔computational join over an open descriptor
vocabulary and is a multi-year build. norr-her-meta sits between them: it
answers "what do the numbers say, and do the computational descriptors track
the experimental ones" for a fixed measurand list on the densest cells. It
is publishable on its own (a quantitative meta-analysis of NORR/HER
electrocatalysts is an ordinary paper type), and it produces three things
the review needs anyway: a human-adjudicated gold set, the count of distinct
(measurand, reference state, convention) triples behind one descriptor (the
number the descriptor-graph section says decides a 3-month vs 2-year build),
and the first exp↔comp join on real data.

## Step 1 result — hub census (2026-09-27, zero model cost)

Regex census over the 1664 hub sentences (`~/.claude/projects/-Users-reto-precis-mcp/norr-her-meta/census.py`, output `census.txt`). 87% of hubs carry a digit.
Hubs per measurand bucket (a hub can hit several):

| bucket | hubs |
|---|---|
| current density (mA cm⁻²) | 280 |
| Faradaic efficiency (%) | 208 |
| adsorption / free energy / barrier (eV) | 188 |
| potential with a stated reference (vs RHE/SHE/…) | 185 |
| potential with NO stated reference (bare V) | 150 |
| overpotential (mV) | 150 |
| NH3 yield rate (mol or g h⁻¹ cm⁻² / g⁻¹) | 133 |
| stability (h) | 102 |
| Tafel slope (mV dec⁻¹) | 81 |
| temperature | 47 |
| explicit limiting/onset potential | 3 |

Densest cells (mode × side, hub count, top buckets):

- `expt-electrochemical × h2-generation` n=351: current density 157,
  overpotential 119, Tafel 64. Its computational partner `dft ×
  h2-generation` n=152 has 53 energy hubs (ΔG_H*). **This is the only cell
  pair where both sides of the exp↔comp join are dense in the hubs.**
- `expt-electrochemical × nh3-formation` n=180 + `mixed × nh3-formation`
  n=107: FE 165, potential-vs-ref 119, yield rate 102. Its partner `dft ×
  nh3-formation` is n=50 with ~25 numeric hubs, limiting potentials mostly
  written as bare V. The NORR exp↔comp join is thin on the computational
  side *in the hubs* — the DFT papers hold numbers the readers did not
  surface as claims, so this is a hub-coverage fact, not a corpus fact.
- `expt-electrochemical × stability-feed` n=105: stability hours 36.

Two identity facts the census already shows: (a) 150 hubs state a potential
with no reference electrode against 185 that do — the reference-state
problem from the descriptor-graph section is visible at claim level; (b) the
`nh3-formation` side pools NO gas, nitrite, nitrate and plasma-NOx feeds,
and **feed is not a tag** — it must be a categorical qualifier in the
measurand list or the pooled FE numbers are incomparable.

## In scope

Sequence decided 2026-09-27; steps 3–5 are the work.

1. ~~Hub census~~ — done above.
2. **Fix the measurand list from the census.** Proposed (closed, with
   declared reference states / conventions per entry):
   NH3 Faradaic efficiency (%, at stated potential); NH3 yield rate
   (canonical µmol h⁻¹ cm⁻², with the mass-normalised form kept separate,
   never converted); applied potential (V vs RHE; SHE/SCE/Ag-AgCl converted
   only when pH is stated, else kept with its reference); HER overpotential
   at 10 mA cm⁻² (mV) and Tafel slope; ΔG_H* (eV, with functional and
   reference state); NORR limiting potential (V, with the potential-
   determining step named); stability duration (h, with the retained-FE
   criterion). Qualifiers on every value: feed (NO gas / nitrite / nitrate /
   plasma-NOx / none for HER), electrolyte + concentration, pH, cell type,
   catalyst family tag. Categorical qualifiers are stored, not inferred.
3. **20-paper round on the pilot as is.** Papers: supporters of hubs in the
   two dense cell pairs, split ~12 experimental / ~8 DFT, chosen so each
   experimental paper has a DFT counterpart on the same catalyst family.
   Chunk text pulled from prod (read-only) into the persistent scratch dir.
   Deliverables: the distinct-triple count per measurand, unit variance per
   measurand, the first gold set (Reto adjudicates), and a list of which
   quantbind items 2–7 actually bit on these papers.
4. **One figure.** NH3 FE vs applied potential (V vs RHE) by catalyst
   family, feed as marker, beside DFT limiting potentials for the same
   families; and/or HER overpotential@10 vs ΔG_H* by family. If it holds,
   the paper has a spine and quantbind item 8 becomes justified engineering.
5. **Draft `norr-her-meta`** in precis, citing the survey hubs and the
   extracted values with chunk-anchor provenance.

## Explicitly NOT in scope

- **Porting quantbind to `src/precis/` (item 8) before step 4.** The round
  changes the binding format (items 2, 3, 5, 5b, 6) and decides the
  kind-vs-finding question; porting first means porting twice. Trigger for
  the port: the first figure has to be cited from a draft.
- **Open descriptor vocabulary, unit conversion engine, entity resolution,
  subsumption widening** — the descriptor-graph section. This item uses a
  closed list and the existing tags.
- **Scientific adjudication by the machine.** Values stay proposals until
  Reto adjudicates the gold set; nothing signs or publishes.
- **The whole corpus.** Two cell pairs. Cells with <30 numeric hubs are out.

## Acceptance criteria

1. A measurand list document exists with, per entry: canonical unit,
   allowed reference states, conversion rule or "never convert", required
   qualifiers. Reto has signed it off before the round starts.
2. The 20-paper round reports, per measurand, the number of distinct
   (measurand, reference state, convention) triples and the units seen, in
   a file under the scratch dir and summarised in this item.
3. A gold set of ≥100 adjudicated values with chunk anchors exists, with an
   agreement rate between reader output and adjudication reported per
   measurand — the accuracy figure the pilot never produced.
4. One figure renders from the gold set alone, with every point traceable
   to a paper ref and chunk anchor.
5. Nothing in this item writes to prod except the eventual draft chunks and
   findings through the existing write door.

## Target + blast radius

Reads: prod `refs`/`ref_tags`/`tags`/`links`/`chunks` via `scripts/prod-psql`
(read-only). Runs: `paper-extraction-pilot/` standalone. Writes: scratch
dir only until step 5. No `src/precis/` change until the port trigger.

## Open questions / decisions log

- **Decided 2026-09-27:** meta paper is a stage of the review, not a
  parallel track; quantbind port deferred to the first-figure trigger.
- **Open:** which of the two cell pairs leads. HER has the dense exp↔comp
  join in the hubs; NORR is the review's actual topic but its DFT side is
  thin at hub level. Default: run both in the 20-paper round and let the
  triple count decide.
- **Open:** does a reader round on the 20 papers use the pilot's format v4
  unchanged, or apply item 2 (condition role) first since feed/electrolyte
  are exactly the conditions that role disambiguates.
