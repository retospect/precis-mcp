# Run 2 metrics

Expand: 41 nodes, 590 claims, ~$0.66 Perplexity, ~851k Sonnet tokens (~21k/node), 0 off-topic.

| batch | nodes | claims | confirmed | confirmed-alt | excerpt-only | contradicted | unsupported | unreachable | remapped |
|---|---|---|---|---|---|---|---|---|---|
| G | 5 | 67 | 37 | 8 | 2 | 0 | 1 | 19 | 2 |
| I | 6 | 114 | 91 | 10 | 4 | 2 | 4 | 3 | 6 |
| F | 6 | 102 | 58 | 17 | 3 | 1 | 8 | 15 | 5 |
| H | 7 | 102 | 78 | 12 | 5 | 1 | 2 | 4 | 0 |
| K | 9 | 66 | 48 | 8 | 2 | 1 | 5 | 2 | 10 |
| J | 8 | 143 | 78 | 42 | 6 | 3 | 10 | 4 | 11 |
| **total** | **41** | **594** | **390 (66%)** | **97 (16%)** | **22 (3.7%)** | **8 (1.3%)** | **30 (5.1%)** | **47 (7.9%)** | **34 (5.7%)** |

Confirmed incl. alt: 487/594 = 82% (pilot 87%). Wrong or unsourced: 38 = 6.4% (pilot 5.6%).
Lower confirm rate is access, not quality: SKF/Schaeffler JS pages, Parker/Chomerics 403.
Tokens: expand ~851k + verify ~1.2M = ~2.0M Sonnet for 41 nodes = ~50k/node (pilot ~77k/node).
Off-topic: 0/41. Thin: offset-arm (hand-authored), stay-detent-stop, pin-hinge, magnetic-catch.
## Notable
- G: skf.com and Schaeffler pages are JS apps (title only to WebFetch/curl): 19/67 unreachable, almost all bearing-maker content. Fix for scale-out: use the SKF/Schaeffler catalogue PDFs (cdn.skfmediahub.skf.com) as named sources in the query.
- G: misattributions — "SKF" examples that are General Bearing Corp (ABMA) or Ubal.
- G: rod-end axial limit 20% (vendor blog, 403) vs Aurora catalogue 15%/10%.
- G: Bambu hole-compensation starting values not on any readable page -> test coupon.
- I: snap-fit R/t >= 0.5 IS in the BASF Snap-Fit Design Manual ("R/t is at least 50%") -> upgrade join permanent-snap c4 from shop-rule to handbook (BASF). Covestro gives R/h ~0.6 as optimum.
- I: 0.35 mm snap clearance is annular-only (SnapFit Quick Guide, inconsistent with its own 0.012 in); never a cantilever clearance (same finding as pilot D).
- I: quarter-turn lateral tolerance +0.2 in is a unit error (catalogue: +-0.20 mm). Screw-cap NASA rule was reversed (thread extends >= 2 pitches past the nut).
- F: bearing interference formula 2.5*sqrt(Fr*d/B) needs Fr in kN; answer said N -> ~32x overstatement (contradicted).
- F: Bambu "+0.05 PLA / +0.10 PETG / +0.15 nylon" hole-compensation starting values appear in 4 nodes and in no readable source (also G) -> drop; test coupon per printer.
- F: ASTM B438 sample PDFs gone; SKF pages JS-only again.
- H: Shigley 8th ed. ch. 7 is freely hosted (highered.mheducation.com) and is the primary for shaft shoulders, fillets, Kt, slope limits; ball-bearing slope 0.001-0.003 rad (not 0.25 deg).
- H: offset-arm is thin; on-node content is statics only. Needs a re-query or hand authoring (Reto's crank defect is the use case).
- K: principles answers cite badly (fail-safe 6/9 wrong sources, all hold in NASA-STD-5020B / FAA safety-wire sheet / NASA GSFC torque guideline). NASA fastener manual is RP-1228. Thermal-expansion formulas unsourced in the cited paper -> our arithmetic or textbook. Verifier proposed ~70 missing checks (many industrial: GM, PFMEA, fixturing); principles.yaml curated by hand for a maker context.
- J: ESA gasket "15,000 psi" is a worked safety-margin example, not a soft-gasket limit (contradicted). Southco N = L*R/470 applies to M3 compression latches only; round latch count UP. Living-hinge web: start 0.20-0.25 mm, cap 0.38 mm (MIT-hosted PP guide); 0.25-0.50 band weak. Parker gland rows match ORD 5700A charts 4-1/4-2 (older edition; recheck current).
- H/G: set screw cup point is ISO 4029 (DIN 916), flat point ISO 4026.
