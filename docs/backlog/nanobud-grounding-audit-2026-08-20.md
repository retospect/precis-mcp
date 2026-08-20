---
status: draft
title: "grounding audit of dr173020's 126 cited hubs — 2 contradicted, 7 unauditable, ~10 overreaching"
---

# Do the passages actually say what the claims say?

Run 2026-08-20 over every claim hub cited by the nanobuds draft (`dr173020`),
four read-only agents, one quarter each. Each hub's sentence was read against the
full text of every grounding passage on its evidence edges. This is the check no
gate performs: mint verifies that an edge exists and that the source is primary,
never that the passage supports the sentence.

It was prompted by two HKUST-1 hubs (`fi176432`, `fi177486`) found grounded to a
hardness passage and to a ZIF-8 paper respectively — both with valid-looking
edges. Those are in `dr42995`'s cohort, not this one; **the boxel document has
not been audited and should be next.**

## Result

| verdict | count |
|---|---|
| SUPPORTED | ~106 |
| PARTIAL | ~10 |
| NO_GROUNDING | 7 |
| CONTRADICTED | 2 |
| UNSUPPORTED | 0 |
| JOINTLY_ONLY | 0 |

Counts are approximate by 2–3: the quarters overlapped at their boundaries
(191155/191156 and 191292 were each audited twice) and one batch's own summary
disagreed with its detail table on a ref id. Re-derive before quoting. Zero
`JOINTLY_ONLY` is worth noting — the feared "supported only in conjunction"
case did not appear at all.

## The two contradictions — fix before anything else

- **`fi191323`** — claims field-emission threshold fields of **1–2 V/μm**; the
  passage says **1–2 V/mm**. Three orders of magnitude, and the claim's version
  is physically implausible. A transcription error that survived mint, lint and
  approval. (One batch's summary names a different ref id than its detail table;
  confirm the id before editing.)
- **`fi211523`** — claims the laddering effect has been shown "only in molecular
  dynamics simulations, not on physically fabricated junctions". Its own source
  paper presents a method for producing the junctions and states the effect is
  exhibited in that work. The claim asserts a negative about the literature that
  the cited paper refutes.

## Seven hubs cannot be audited at all

`fi191292`–`fi191296` carry **no evidence edges whatsoever** despite being cited
by the draft. `fi190978` and `fi191002` have ref-level edges with
`src_chunk_id IS NULL`, so there is no passage to read. Either way the draft
cites claims whose support cannot be inspected.

## The failure class worth naming

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

## Caveat on the method

Verdicts are LLM judgments over passage text, advisory and unreviewed. They are
detection, not truth: no write was made and none should be made from this file
without a human reading the passage. The value is that it narrows 126 hubs to
about a dozen worth a person's attention.
