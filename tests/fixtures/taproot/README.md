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
- **Regression pairs** (pair 239 onward, `provenance: regression:<gripe>`):
  real hub pairs a human merged after the mint path forked them. Pair 239
  is fi449493 / fi192855 (gr477211), a landscape restatement labelled
  `equivalent`. The mint-time identity fix lives in
  `taproot/hub.py::_find_restated_hub`; this row tracks whether the judge
  also catches the paraphrase, which identity cannot.

## Known gaps
- `contradicts` coverage is now mostly **synthetic** (n=22 of 23) — fine
  for testing judge *logic*, but not evidence of real literature disputes.
- **Domain skew** toward nanoelectronics + NOx/catalysis (the corpus's
  actual mass).
- 238 pairs is a v1 bar; expand as canonicalization is built against it.

## `extraction_passages.jsonl` — the AIDA-Atomic gate + pre-run canary

Lives at `src/precis/data/taproot/extraction_passages.jsonl` (moved out of
`tests/fixtures/` in round 2 so it ships in the wheel — the canary,
`precis taproot-migrate canary`, runs it on a deployed host before any
bulk dry-run; `eval_canon.EXTRACTION_PASSAGES_FIXTURE` is the canonical
path). A separate, smaller fixture for `canon.extract_claim`'s
decomposition step (`eval_canon.eval_extraction` +
`eval_canon.canary_extraction`), not the dedup gate above. One object per
row: `id`, `passage`, `expected_atom_count` (0 for NO-CLAIM),
`expected_not_claims` (rejected-conjunct substrings, audit only — not
graded as a hard gate in v1), `note` (provenance / rationale), and — on the
causal rows only — `expected_explains_count` (how many `explains` pairs the
passage warrants; graded by `eval_canon.canary_extraction` as a SOFT metric,
never failing the canary, since whether a mechanism clause stands alone is a
granularity call). Thirteen rows, hand-authored (2026-08-13/14, plus the two
causal rows 2026-10-03) to cover: the carbon-nanomaterials worked
example (`docs/backlog/taproot-atomic-claims.md` §Worked example) —
multi-atom + not_claims + surviving composite; an already-atomic claim (no
composite); two NO-CLAIM shapes (pure pointer, meta-prose); a
not-claims-only row (zero atoms survive but rejects are still recorded); a
clean 2-atom split; a legitimate bare-`and` condition list that must
*not* be treated as an un-split atom (regression guard for
`eval_extraction`'s lexical conjunction heuristic); the one-atom+
reject edge case that keeps a composite despite a single surviving atom; and
a P1-6 modality regression (fi176422, `docs/backlog/
taproot-migration-extraction-quality-gates.md`) — a counterfactual
`whereas ... would` foil that a prior prompt version promoted into a false
indicative claim, and must instead stay out of `claims` entirely.

The two causal rows (ids 12-13, 2026-10-03, the `explains` decision) pin the
"X because Y" rule in `canon._EXTRACT_PROMPT`: a self-contained, groundable
mechanism becomes its own atom plus one `explains` pair (id 12: 2 atoms, 1
pair, no not_claims, original sentence = composite); a mechanism too vague to
stand alone ("because of its pore structure", id 13) stays inline in the single
effect atom (1 atom, 0 pairs, never in not_claims).

## `migration_pilot_25.jsonl` — the labelled 25-hub gate calibration

One row per pilot hub: the sentence, the extraction (`atoms`, `composite`,
`not_claims`, and — where the sentence has a causal structure — `explains`,
0-based `[explaining atom, explained atom]` pairs into `atoms`), the model's
verdict, and `expected_gated_verdict` (what `migrate.classify_extraction` must
say; asserted by `tests/test_taproot_migrate_gates.py`, with four accepted
`pass-through` false positives marked strict `xfail` there).

fi176422 and fi176399 (the two "because/arises from" rows P2-13 had kept
folded) were **re-labelled 2026-10-03** under the `explains` rule: they now
carry the split the rule asks for — mechanism atom + effect atom + the
`explains` pair, composite kept, fi176422's `whereas ... would` foil in
`not_claims` — and expect `split`. Those two rows are the labelled
expectation, not the recorded pilot output (the pilot's extraction for
fi176422 minted the foil as a false indicative atom; for fi176399 it dropped
"negligible alone" and "~10^9 setae per foot" and gated `lossy`). The xfail
set is unchanged (still the four pass-through rows).
