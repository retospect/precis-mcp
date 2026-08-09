# Taproot canonicalization fixture

Ground-truth for the Phase-1 claim-canonicalization gate in
`docs/backlog/taproot.md`. 200 pairs of real corpus claims, each
labeled with one lattice relation. The eval target is **over-merge rate
→ ~0** (a false `equivalent` is the dangerous error); under-merge is
tolerated.

## Files
- `claim_pairs.jsonl` — the fixture. One object per pair:
  `pair_id, ref_a, ref_b, claim_a, claim_b, embed_dist, relation,
  provenance, opus, fable[, multi_assertion, note]`.
- `labels_opus.json`, `labels_fable.json` — the two independent model
  labelings, kept for audit.

## Relations
Labels use the full 5-relation vocabulary: `equivalent` (same claim, same
scope) · `broader`/`narrower` (subsumption, directional: `broader` = ref_a
more general than ref_b) · `orthogonal` (distinct claims) · `contradicts`
(same scope, opposite polarity).

**v1 grades collapsed.** The shipped canonicalization is *flat dedup*
(broader/narrower deferred to v2), so v1 maps labels to three verdicts:
`equivalent`→**same**, `broader`/`narrower`/`orthogonal`→**different**,
`contradicts`→**contradicts**. Primary metric: **over-merge rate ~0**
(a `same` that should be `different`). The richer labels are retained so
the same fixture serves a future v2 that restores the hierarchy.

## How it was built (2026-07-28)
1. Drew from `kind='citation'` claims in `precis_prod` (clean world-claims;
   `kind='finding'` was **not** used — it is polluted with editorial
   review notes, see the proposal's open issues).
2. Paired by **nearest-neighbor** over the bge-m3 card embeddings — the
   closest pairs are the equivalent/subsume boundary, i.e. the over-merge
   stress zone. Top 200 by cosine distance (0.0001–0.36).
3. **Two independent labelers** (Opus and Fable), same rubric, blind to
   each other. A model-generated key alone would be circular — a labeler
   that over-merges writes a key that rewards over-merging — so a human
   adjudicates the split, and the models only do the labor + triage.
4. **92% agreement** (184/200). The 16 disagreements clustered into 3
   boundary-policy questions, adjudicated by rule:
   - **A · bundled extra assertion** (10 pairs, `provenance:
     adjudicated:A-split`, `multi_assertion:true`): one side asserts X, the
     other X∧Y. Resolved `equivalent` on the shared core — and it revealed
     that **canonical-form must atomic-split multi-assertion chunks
     first** (a Phase-1 spec addition).
   - **B · shared definition/constant** (3, `adjudicated:B-shared-def`):
     same G₀ / GC-DFT method, different substantive claim → `orthogonal`.
   - **C · principle vs instance/formula** (3, `adjudicated:C-genus-species`):
     qualitative rule vs its formula/instance → `broader` (genus ⊃ species).

238 pairs total. The 16 model-disagreements were **human-adjudicated and
signed off 2026-07-28** (`human_approved` field on those rows).

## Augmentation (2026-07-28)
- **Corpus contradiction pass** (pairs 201–208): an Opus scan of all 422
  claims found only **1 genuine** same-scope contradiction (AGNR
  metallic-vs-semiconducting, borderline) + **7 apparent-but-orthogonal**
  negatives (opposite-sounding, distinct scope) — the corpus is genuinely
  thin on real contradictions (restatements + different-scope opposites
  dominate).
- **Synthetic contradictions** (pairs 209–238, `provenance:
  synthetic:*`, all `needs_adjudication` — spot-check the constructions):
  22 **scope-matched negations** of real claims (hold material/method/
  regime, flip polarity → `contradicts`) + 8 **scope-shifted negatives**
  (shift one scope axis so the opposed-sounding claim is `orthogonal`, not
  a contradiction — hard negatives for open #8). Spread across all five
  domains. These give the `contradicts` edge real coverage (n=23) that
  corpus mining couldn't.

## Known gaps
- `contradicts` coverage is now mostly **synthetic** (n=22 of 23) — fine
  for testing judge *logic*, but not evidence of real literature disputes.
- **Domain skew** toward nanoelectronics + NOx/catalysis (the corpus's
  actual mass).
- 238 pairs is a v1 bar; expand as canonicalization is built against it.
