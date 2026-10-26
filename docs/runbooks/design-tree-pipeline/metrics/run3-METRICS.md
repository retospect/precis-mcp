# Run 3 (S6 processes) metrics

Expand: 53 nodes in 11 batches plus 2 re-queries (anodising, heat-treatment drifted into generic CNC rules under the
DfM template; re-asked with topic templates). Perplexity ~$0.87 (plus one forging call that timed out at 120 s and may be
billed). Sonnet ~1.46M tokens expand + ~2.44M verify = ~3.9M (~74k/node).

| batch | claims | confirmed | confirmed-alt | excerpt-only | contradicted | unsupported | unreachable | dropped | remapped |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| L | 113 | 89 | 16 | 3 | 0 | 4 | 1 | 0 | 10 |
| M | 115 | 95 | 6 | 4 | 3 | 7 | 0 | 0 | 1 |
| N | 96 | 85 | 8 | 1 | 1 | 1 | 0 | 0 | 16 |
| O | 65 | 58 | 2 | 3 | 0 | 2 | 0 | 0 | 7 |
| P | 106 | 87 | 7 | 4 | 5 | 1 | 2 | 0 | 8 |
| Q | 145 | 130 | 0 | 0 | 2 | 4 | 4 | 5 | 0 |
| R | 129 | 108 | 7 | 1 | 6 | 7 | 0 | 0 | 12 |
| S | 100 | 63 | 10 | 4 | 4 | 14 | 5 | 0 | 0 |
| T | 56 | 32 | 6 | 8 | 1 | 6 | 3 | 0 | 11 |
| U | 141 | 119 | 7 | 2 | 7 | 5 | 1 | 0 | 30 |
| V | 115 | 91 | 17 | 0 | 2 | 5 | 0 | 0 | 20 |
| T2 | 61 | 34 | 10 | 4 | 0 | 10 | 3 | 0 | 0 |
| **total** | **1242** | **991** | **96** | **34** | **31** | **66** | **19** | **5** | **115** |

Confirmed incl. alt: 87.5% (pilot 87%, run 2 82%). Wrong or unsourced: 7.8% (pilot 5.6%, run 2 6.4%). Unreachable 1.5%
(run 2 7.9%: no JS-only bearing-maker pages this time). Remapped 9.3% (run 2 5.7%): Perplexity's inline [N] labels often
name a different publisher than its own source list, and cite indices past the end of the list.

## Notable
- Source quality is worse than runs 1-2: an unrelated wellness blog cited for thermoforming and urethane numbers, GitHub
  rule sheets and a Hugging Face dataset for FDM, a Google Groups spam post for resin shrinkage. Verifiers replaced them
  with Prusa/Bambu pages, Prusament/Polymaker TDS, BASF/Covestro datasheets, NADCA, FIA, NASA PRC-5006/5007, ISO preview PDFs.
- The DfM template's standards list (ISO 2768/286/20457) pulls answers toward machining tables: ISO 2768 appeared in 9
  nodes and was wrong in 4 (class m +-0.1 vs +-0.2 at 6-30 mm; +-0.5 vs +-0.3 at 30-120; class H given as K). Fix: one
  shared ISO 2768-1 table (data file), rules point at it.
- Errors caught (each would mis-size a part): texture draft 0.4 deg per 0.1 mm (Hubs) vs 1.5 deg per 0.025 mm (~15x);
  PC minimum wall 0.11 mm (typo for 1.02); PP wall copied from PC row; DIN 472 groove = ring free diameter (21.5 vs 21.0);
  FDM length error 0.051 mm/mm (was mm per inch); "0.8 mm desktop wall" from an industrial 0.254 mm road; waterjet tab
  3.2 vs 1.6 mm; forging external draft 3-5 vs 5-7 deg; die-closure area 650000 cm2 vs 650 x 1000 mm2; thermoforming draw
  ratio 1.5/3 vs 3/4; PETG/HIPS forming window.
- Heat treatment is the weakest node (14/30 confirmed): 12 claims cited a missing source; case depth and nitriding
  temperature ranges disagree across sources. Shipped as shop-rule, red-listed.
- Off-node drift: FDM asides in milling answers, moulding rib rules in sinker EDM, generic CNC/ISO tables in coating
  answers (28 claims removed, listed in REVIEW-3 (d)).
