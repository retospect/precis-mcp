# Pilot protocol: pre-fill join from Perplexity, then verify

Skeleton: `join-skeleton.yaml`. 33 nodes marked `expand: true`.

## Shared vocabulary (use these names only)

criteria: separable{never,service,frequent}, load_kind{static,cyclic,shock}, load_N, torque_Nm,
material_pair, far_material{metal,thermoplastic,thermoset,wood,composite,ceramic},
process{FDM,SLA,moulded,machined,sheet,cast,bought}, access{one-side,both-sides,none},
quantity, sealing, alignment_mm, temp_range_C, environment{indoor-dry,humid,outdoor,salt,chemical},
vibration, cost_weight{low,med,high}, brittle (material is brittle / notch-sensitive).

failure modes: overload, fatigue, creep-preload-loss, loosening, wear-fretting, galvanic-corrosion,
crevice-corrosion, stress-corrosion, thermal-mismatch, softening, environmental-degradation (UV,
moisture, chemical), environmental-stress-cracking, embrittlement (hydrogen, cold, UV),
brittle-fracture, buckling, impact, excess-deflection, assembly-error, lost-part.

axes: locking, captive, corrosion (always applies to metal/metal), bearing-face, material-direction.

## Stage 1: expand (one agent per batch)

For each node: ONE call
`get(kind='perplexity-reasoning', q=<template>)`, then drain every `more(cursor=...)` page
sequentially (never in parallel). Record the `cite as perplexity-reasoning:<id>` and the
`[cost: ~$X]` trailer. No other precis calls. Never put/edit/tag/link/delete. No deep research.

Template (fill <NAME> and <CONTEXT>):

> Mechanical design, joining two parts: <NAME> (<CONTEXT>). Give concisely, with a citation for
> every number: (1) when to choose it and when to avoid it, in terms of separability, load kind and
> size, material pair, manufacturing process (FDM print, moulding, machining, sheet metal, casting),
> access, quantity and environment; (2) main variants; (3) tradeoffs vs alternatives; (4) common
> design mistakes and their fixes; (5) design numbers with units (dimensions, ratios, clearances,
> limits, strengths) and the standard, manufacturer design guide or textbook they come from, prefer
> those over blogs; (6) relevant failure modes (fatigue, corrosion, creep, brittle fracture,
> loosening, wear) and how to design them out.

For an axis node, replace the first sentence with: "Mechanical design, fastened and fitted joints:
the choice of <NAME> (<CONTEXT>)."

Output: `<drafts>/pilot/draft/<batch>.yaml`, a list of:

```yaml
- id: join/service/threaded/clearance-tapped     # path from the skeleton
  pplx: perplexity-reasoning:<id>
  cost_usd: 0.014
  summary: <1-2 lines, OUR words>
  when: [<predicate over the criteria above, plain text ok>]
  avoid_when: [...]
  variants: [...]
  tradeoffs: [...]
  howto: [<3-8 imperative lines, our words>]
  issues:
    - {issue: ..., fix: ..., failure_mode: <from list>}
  axes: {locking: <relevant|n/a: reason>, captive: ..., corrosion: ..., bearing-face: ..., material-direction: ...}
  claims:                       # EVERY number or hard rule, one entry each
    - id: c1
      text: <the claim, our words>
      value: <number or range>
      unit: mm
      source_n: 3               # the [N] in the Perplexity answer
      source_url: <url from the answer's source list>
      source_kind: standard|manufacturer|textbook|academic|vendor-blog|forum|wiki|other
```

Write all prose in our own words; do not paste Perplexity sentences.
Also write `<drafts>/pilot/draft/<batch>.log` with one line per call:
`<node id>\t<pplx id>\t<cost>\t<seconds>`.

## Stage 2: verify (a different agent per batch)

Stage 1 agents saw folded pages (the MCP folds at 4 KB), so drafts miss content and some
`source_n` -> URL mappings are wrong. The full answer for each node is in
`<drafts>/pilot/raw/<pplx id>.md` (read it with the Read tool, not the MCP).

Step 0, before verifying: read the node's raw answer in full.
- Fix each claim's `source_url` from the answer's own source list (`[N] url`), note `remapped`.
- Add claims the draft missed (any number or hard rule), with `added_in_stage2: true`.
- Add missing variants/tradeoffs/issues the same way. Our own words only.

For each claim: fetch `source_url` with WebFetch (no precis calls). Verdict:
- `confirmed`: the source states the value (or a range containing it) for this context.
- `contradicted`: the source states a different value. Record what it says.
- `unsupported`: the source loads but does not state it.
- `unreachable`: paywall, 404, PDF too large.
For `unsupported`/`unreachable` with source_kind not standard: one WebSearch for a primary source;
if found and it states the value, verdict `confirmed-alt` with the new url.
Record `quote` (<= 25 words from the source) for confirmed/contradicted, and `source_quality`
(primary|secondary|weak).

Output: `<drafts>/pilot/verified/<batch>.yaml`, same records with per-claim
`verdict, quote, source_quality, alt_url, note` added. Plus a footer:
counts per verdict, and the 5 claims that matter most for a designer that failed.
