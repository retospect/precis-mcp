# Taproot canonicalization fixture

Ground-truth for the Phase-1 claim-canonicalization gate in
`docs/proposals/taproot.md`. 200 pairs of real corpus claims, each
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
`equivalent` (merge — same claim, same scope) · `broader`/`narrower`
(subsumption, directional: `broader` = ref_a more general than ref_b) ·
`orthogonal` (distinct claims) · `contradicts` (same scope, opposite
polarity). Rubric: the labeling prompt in the proposal's Phase-1 notes.

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

## Augmentation (2026-07-28) — contradiction pass
An Opus scan of all 422 citation claims for same-scope opposite-polarity
pairs (pairs 201–208): only **1 genuine** contradiction found (AGNR
metallic-vs-semiconducting, `needs_adjudication`, borderline coarse-vs-
refined), plus **7 apparent-but-orthogonal** negatives (opposite-sounding
but distinct scope — Au on pristine-vs-defect graphene, UiO-66 saline-vs-
phosphate, DFT-vs-GW gaps, …). Those 7 are valuable: they exercise the
contradiction-vs-scope-mismatch boundary (proposal open #8).

## Known gaps
- **The corpus is genuinely thin on real contradictions** — dominated by
  duplicate/restated claims and opposite-sounding pairs that resolve to
  different scope. So the `contradicts` edge is under-covered (n=1) and a
  real Phase-1 gate will need **synthetic contradictions** (negate real
  claims at matched scope), not just corpus mining.
- **Domain skew** toward nanoelectronics + NOx/catalysis (the corpus's
  actual mass), not an even MOF/Pd/NOx split.
- 208 pairs is a v1 bar; expand as canonicalization is built against it.
