# Pilot metrics (filled as verifiers report)

| batch | nodes | claims (draft+added) | confirmed | confirmed-alt | contradicted | unsupported | unreachable | remapped |
|---|---|---|---|---|---|---|---|---|
| D | 6 | 89 (77+12) | 59 | 17 | 2 | 2 | 9 | 0 |
| C | 6 | 88 (77+11) | 69 | 9 | 2 | 4 | 4 | 4 |
| B | 5 | 96 (79+17) | 75 | 7 | 1 | 5 | 8 | 6 |
| A | 7 | 120 (86+34; cast-as-one rebuilt) | 93 | 11 | 2 | 2 | 12 | 5 |
| E | 9 | 141 (114+27; second-fastener rebuilt) | 96 | 28 | 3 | 7 | 7 | 8 |
| **total** | **33** | **534 (433+101)** | **392 (73%)** | **72 (13%)** | **10 (1.9%)** | **20 (3.7%)** | **40 (7.5%)** | **23 (4.3%)** |

Confirmed incl. alt: 464 / 534 = 87%. Wrong or unsourced (contradicted + unsupported): 30 = 5.6%.
Off-topic answers: 2 / 33 (cast-as-one, second-fastener), fixed by one re-query each. 1 node too broad (clinch-crimp-swage -> split in 4).
Perplexity cost: ~$0.60 total (~$0.018/node), incl. 2 re-queries and 3 stray test calls.
Agent effort: expand ~766k Sonnet tokens (most spent paging folded output), verify ~1.78M. ~2.55M Sonnet tokens total, ~77k tokens/node end to end. At 3,000 nodes that is ~230M tokens: agent time, not Perplexity, is the cost driver.
Wall time: ~10-15 min per batch per stage, batches in parallel: ~45 min total.
Step 0 added 101 claims (19%): the 4 KB fold hid that much from the expand agents.

## Pipeline changes for scale-out
- Dump raw answers to files right after each call (no more() paging) -> expand cost should roughly halve.
- Merge expand + Step 0 into one agent reading the raw file.
- Templated query: put the node name first (cache slug collision, gr517283).
- Verifiers need local PDF text extraction (WebFetch cannot read compressed PDFs).
- "confirmed-alt from search excerpt only" is its own weaker verdict.
## Notable failures
- D releasable-snap c7: Perplexity presented a friction-coefficient table (Covestro Table 3) as snap-fit strain limits. Wrong quantity, plausible numbers. Covestro strain table covers PC-family only; other resins via yield-strain rule.
- D retaining-ring c5: load capacity taken from the wrong ring type/size row (DIN 471 25 mm vs DIN 472 20 mm).
- C through-bolt c10: "NASA 0.25 V galvanic limit" — not NASA; 0.25 V is a MIL-STD-889 anodic-index tier. Misattribution.
- C through-bolt c1: ISO 273 scope wrong (1-150 mm per ISO abstract, not 1.6-39).
- C nut-trap c10: boss OD rule merged two sources (2-2.5x hole dia vs 2.5x screw dia).
- C thread-forming: Bossard/EJOT primaries 403/404; numbers confirmed via third parties only.
- B permanent-snap c4: R/t >= 0.5 root radius — no source states it (common folklore number). c3 DuPont strain table unreachable.
- B rivet c10-c12: wrong document (aerospace flush rivet sheet) and wrong rivet size; FAA AC 43.13-1B 403.
- B clinch howto had the sheet order reversed vs source; node should split into 4 leaves.
- B tooling: WebFetch can't read compressed PDFs; verifier used ghostscript text extraction.
- A one-body c7: texture draft 1.5 deg per 0.025 mm (not 0.25 mm) — 10x under-draft; source contradicts itself.
- A cast-as-one c1-c2: process-selection quantity/size thresholds unreachable; die-cast size limit contested.
- A: 5 confirmed-alt rest on search excerpts only (host 403/401) — weaker than "read the page".
- A machine-from-solid c8: rib 50-60% of wall, not 40-60%.
- E corrosion c4b: source prints the galvanic area ratio inverted (10:1 cathode:anode "optimal"); direction rule settled from course notes (small anode + large cathode is bad); no primary for 10:1.
- E nest c4: FDM "press fit 0-0.1 mm" is a clearance, not an interference — a press fit designed from it fails.
- E second-fastener n11b: floating vs fixed fastener formulas conflated; check ASME Y14.5.
- E bearing-face c6/c7/c9/c15: limit pressures not in cited sources; use VDI 2230 table directly.
- D twist-lock c11: NASA-STD-5020B e/D >= 1.5 is an applicability criterion, not a minimum edge distance.
