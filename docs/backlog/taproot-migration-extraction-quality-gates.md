---
status: in-progress
title: Taproot migration — phase-0 scoring bug + extraction quality gates before the 1.3k run
model: opus
---

> **2026-08-14: all 13 items IMPLEMENTED and shipped** — P0/P2 in
> `taproot/migrate.py` + CLI (`lossy`/`nested` verdicts, body-sentence
> scoring, `--json` persist, seeded random controls, `junk_candidate`,
> `escalate_fn`), P1/P2-13 in `taproot/canon.py` (enumerate-then-emit,
> modality + mechanism rules, compound synthesis, scope validation,
> `extract_claim_strict_big`). Calibration fixture:
> `tests/fixtures/taproot/migration_pilot_25.jsonl` — 22/25 exact,
> 3 xfail (correct pass-throughs lexically indistinguishable from lossy;
> gate errs toward flagging).

## Round 2 — labelled-25 A/B re-run on melchior (2026-08-14) + fixes

**A/B result (SMALL leg, deployed gates, pure per-sentence driver over the
fixture's stored sentences).** The gates work: the pilot's dangerous class
— 6/25 silently stamped while still compound — is now **0/25**; every
defective extraction was flagged `lossy` (P2-12 blocks stamping those).
But the extractor regressed: **SMALL (glm-4.7-flash) + the
enumerate-then-emit prompt collapses nearly every multi-clause sentence to
a single truncated atom** (22/25 landed in the escalate set; the pilot's
old prompt split 9/25 correctly). SMALL also now hallucinates atoms out of
task-prose/junk hubs (3 expected-no-claim rows came back as 1-atom lossy)
and, once, an invented measurement ("10^208", fi201713). One gate false
negative surfaced: fi176441's truncated re-extraction dropped a predicate
yet cleared the pass-through recall ratio at 0.765. One genuine
improvement: fi176365's new 2-atom split cleanly partitions its sentence
(the old pass-through label was the pilot's, not ground truth).

**Decisions (Reto):** BIG tier becomes the primary extractor; the SMALL
prompt is not worth tuning; fix the gates; skip the structural-truncation
flag (the coverage gates already catch that shape).

**Implemented (this round):**
- **BIG primary** — `dry_run()` and the CLI default to
  `extract_claim_strict_big`; SMALL is opt-in (`--tier small`), and
  `--escalate` (the SMALL→BIG retry) is rejected with the BIG primary.
- **Canary** (`precis taproot-migrate canary`) — the 11 hand-authored
  passages (now packaged: `src/precis/data/taproot/
  extraction_passages.jsonl`) through the chosen tier + the migration
  gates; exit 1 on any `lossy`/`nested` or no-claim mismatch. Run before
  every bulk dry-run: catches a collapsed extractor for 11 calls instead
  of a burned run.
- **Missing-content-word cap** (pass-through only, ≥4) — catches the
  fi176441 class the recall ratio is too coarse for; costs one new
  documented fixture false positive (fi176361, now the 4th xfail).
- **Hallucination gates** — invented number tokens (mirror of the
  missing-number gate) and content-word precision < 0.8 are hard `lossy`;
  recall alone is blind to added material (fi201713, fi177406, fi176275).

**BIG-leg A/B result (escalation pass over the 22 SMALL-failed hubs,
2026-08-14).** BIG-as-primary validated: 8/22 sound (6 split + 2
pass-through, incl. fi177406 whose pilot extraction invented "113" — now
clean), 3 nested (gate blocks them, hub untouched), **zero lossy** — no
silent damage anywhere. BIG also correctly refuses all 5 junk/task-prose
hubs SMALL hallucinated atoms for. 1 retryable dispatch timeout
(fi176812).

**Watch-item RESOLVED — the false no-claims were mechanical.** ~4/22 real
claims came back `no-claim` with a completely empty extraction (no atoms,
no not_claims, no reason). A raw-response probe (4 hubs × 3 repeats
through the same BIG dispatch, capturing text before parsing) showed the
model never once *deciding* no-claim: every failure was a broken output
contract — prose instead of JSON, a self-invented schema
(`adhesion_mechanism`/`setae_count`…), or a literally empty reply — each
parsing to `_EMPTY_EXTRACTION` and reading as a silent NO-CLAIM. Mostly
flaky (2 hubs 3/3 clean on re-probe), but fi176399 (dense inline data:
~200 nm, ~10⁹ setae) derailed the BIG chain 3/3. The chain also failed
over local deepseek-v4-flash → cloud glm-5.2 mid-probe, so "BIG" is two
backends. A control probe of the same 4 prompts × 3 repeats on
**claude-haiku held the JSON contract 12/12** — including the gecko
sentence — proving the prompt is fine; the flake is model-specific.

**Round 3 (Reto, 2026-08-14): haiku becomes the primary extractor.**
Implemented: `extract_claim_strict_haiku` — a documented router-bypass
(`call_claude_p` + `resolve_model(Tier.MEDIUM)`, the `fix_gripe` pattern;
listed in `EXCLUDED_OPERATIONS`) because inside `dispatch()` an operator
`llm.chain.<tier>` rung's model beats a call-site pin, and prod pins
`llm.chain.medium` to an OSS model. Format-flake guard, two retryable
shapes (reviewer-corrected — `call_claude_p` RAISES on a prose/empty
reply, it never returns one): unparseable reply
(`ClaudePUnparseableError`, new subclass) retried once then
`ExtractionUnavailable` (persistent prose mode is infra-grade, never a
NO-CLAIM); empty-empty JSON retried once then accepted as genuine
NO-CLAIM (pointer prose). Guard deliberately NOT in the per-chunk SMALL
backfill path where legit no-claims are common. Accepted blind spot
(fix_gripe precedent): the lane rides the OAuth subscription and writes
no `llm_call_log` rows — invisible to the budget breaker; per-call
`max_usd` cap + debug cost log only. CLI + `dry_run()` + canary default
`--tier haiku`; `small`/`big` remain explicit opt-ins; `--escalate` now
rejected with any non-SMALL primary. Third retryable shape added after
the first local canary run: the `claude` CLI intermittently exits 1 fast
with empty stderr — a `ClaudeProcessError` carrying a real `returncode`
is retried once; a timeout/spawn failure (`returncode is None`) still
raises immediately. Per-call cost measured on the 11-passage fixture:
$0.044–0.069, comfortably under the $0.10 `max_usd` cap.

**Remaining before the 1.3k (pivot 2026-08-14: run IN-SESSION on the
Mac, no deploy needed — `claude -p` + OAuth are local and the dry-run
writes zero prod rows):** canary local (GREEN 2026-08-14, 0 rejections /
0 mismatches) → ~100 hubs local (DONE, below) → full 1,346 dry-run.
**Reto authorized production writes 2026-08-14**; phase-2 apply mode is
BUILT + reviewed (`apply_migrate.py`, `taproot-migrate apply`) — it
treats `no-claim` on an evidenced hub as `needs_review`, never a
skip-stamp. Consider re-labelling fi176365 (`pass-through` → `split`).

**100-hub run (top-scored = hardest cohort, haiku, 2026-08-15) + gate
round 4.** Raw verdicts: 26 split / 1 pass-through / 6 no-claim / 46
lossy / 15 nested / 6 error. Inspection showed the lossy wall was mostly
gate false positives against *prod* body idioms the labelled-25 fixture
never had: (a) haiku normalizes notation — `10^4-10^6` → `10⁴–10⁶`,
`13-residue` → scope `"13 residues"`, `near 10` → `~10` — breaking
exact-token number membership in BOTH directions; (b) hub bodies embed
citations ("Canonical references: …" tails and unmarked inline "Phys.
Rev. Lett. 57, 1761, 1986" / "(Zhang 2009; Mak 2008)" / "123(24),
5035-5047" spans) that a correct extraction drops but the gates demanded.
Fixes (loss-direction only; the invention direction keeps the full
original as allowlist): unicode superscript/dash/approximation folding +
digit-leading hyphen splitting in `_number_bearing_tokens` (letter-leading
tokens never split — MOF-5 stays excluded; 409/9 substring hole stays
closed), citation tail + inline-span stripping via
`_strip_citation_tail`. Re-gated the SAME 100 extractions offline
(pure-function re-classify, zero LLM cost): **49 split / 1 pass-through /
6 no-claim / 23 lossy / 15 nested / 6 error**. Labelled-25 fixture stays
green with the same 4 documented xfails. Residual 23 lossy = citation
exotica + genuinely messy meta-prose hubs — correctly left untouched.
The 15 nested are real haiku granularity wobble (filler atoms contained
in siblings) — correctly gated. The 6 errors are persistent local
`claude -p` exit-1s (fast, empty stderr) that survive the 5 s-backoff
retry even on a quiet machine — hubs simply stay pending; re-run later or
investigate the CLI failure mode if the rate holds at 1.3k scale.

# Taproot migration: score the claim sentence, gate lossy/nested extractions

Findings from the 25-hub phase-1 pilot (2026-08-14), adjudicated against
un-truncated originals from prod and a second full run. **Verdicts were
identical on 25/25 across two independent runs** — the defects below are
systematic (prompt/contract gaps), not model noise, so each fix is
A/B-measurable against the same 25 hubs.

Applied as-is, the pilot would have been: 16/25 correct, 2/25 writing wrong
structure, 6/25 stamped "migrated" while still compound, 1/25 a good split
needlessly discarded.

## P0 — must land before the full 1,346-hub run

1. **`score_hubs` scores the wrong string.** It scores `refs.title`;
   extraction runs on `migrate._read_claim_sentence` (the `finding_body`
   ord=0 chunk). Measured on prod: the two differ for **572 of 1,346**
   candidate hubs, and **259 hubs (19%)** score atomic by title but
   compound by body (title-scored likely-compound = 281; body-scored =
   **540**). Claim hubs come in two shapes — sentence-titled and
   topic-titled (short label, claim in the body). Fix: LEFT JOIN the
   `finding_body` chunk in `migrate._CANDIDATE_HUBS_SQL` and score that,
   falling back to title. Recompute cohorts.
2. **Coverage gate → new verdict `lossy`; never stamp it.**
   `migrate._classify_extraction` reads "1 atom, no compound" as
   *pass-through: already atomic*. In 6 of 11 pilot pass-throughs the model
   had simply dropped conjuncts and the atom was a strict subset of the
   original (fi176545, fi176764, fi176812, fi176275, fi176360, and control
   fi198257 which dropped the n-type 409 μA/μm figure). **5 of 6 kept the
   *last* clause** — a recency signature. Gate: content-word recall of the
   original by the union of atoms, plus a hard rule that every number+unit
   in the original survives in an atom or scope value. Below threshold →
   `lossy`, excluded from apply, queued for escalation. Without this,
   phase-2's per-hub stamp permanently retires still-compound hubs.
3. **Containment gate → new verdict `nested`.** fi176441 returned A1 ⊂ A2 ⊂
   A3 with **A3 equal to the compound** — 3 hubs minted for 1 claim, and
   self-referential compound trust. Reject atom sets where one atom's
   normalized text contains another's, or an atom ≈ compound (token-set
   ratio > ~0.9). No model call needed.
4. **Persist dry-run outcomes; un-truncate the report.** `migrate.
   render_report` truncates originals and atoms at ~100 chars and outcomes
   are persisted nowhere, so reviewing the pilot required re-running prod
   LLM calls. Write outcomes to a JSONL artifact keyed by (run id, hub);
   add a `--full` rendering mode.

## P1 — extraction-contract fixes (`taproot/canon.py`)

5. **Enumerate-then-emit** in `_EXTRACT_PROMPT`: list the assertions found,
   then emit one atom per listed assertion. Targets the keep-the-last-clause
   loss behind every P0-2 case.
6. **Modality rule.** A clause under *would/could/if/whereas/absent* gets
   its regime qualifier or becomes a `not_claim` — never a bare indicative.
   fi176422 minted "Incompatible catalytic sites neutralize each other in
   homogeneous solution" from the source's *counterfactual foil* ("whereas
   in homogeneous solution they **would** immediately neutralize"). The one
   outright false claim in the batch.
7. **Compound-coverage invariant for splits.** The union of atoms must cover
   the compound. fi177406 dropped the CnaB2-pilin-domain provenance;
   fi176399 dropped both "negligible alone" and "~10^9 setae per foot".
   Since compound trust is worst-of-atoms, a compound containing text no
   atom grounds carries *unearned* derived trust — a soundness hole, not
   just lost yield.
8. **Missing compound → synthesize, don't discard.** `_coerce_extraction`'s
   partial-citation guard degrades atoms≥2-with-no-compound to NO-CLAIM;
   fi177585 lost a good 2-atom split to a formatting miss. The compound is
   available for free: the source sentence *is* the bundle.
9. **Constrain `scope` keys.** fi176359 emitted
   `quantity: "rectangular outline"` / `"pin count conventions"`. Not
   cosmetic — `hub.mint_hub` feeds scope into
   `make_taproot_hub_paper_id`, so junk scope perturbs **hub identity**.
   Validate (e.g. `quantity` must contain a digit) or drop the key.

## P2 — process

10. **Selective escalation, not a blanket BIG-tier bump.** Stable verdicts
    ⇒ systematic errors ⇒ fix the prompt first, re-measure, then escalate
    only `lossy`/`nested`/`no-claim` hubs (~25–30% by this sample).
11. **Control sampling is biased.** Controls come from the score-sorted tail
    of likely-atomic = shortest titles = topic/task hubs; 4 of 5 pilot
    controls weren't claims, so the pass-through check never ran. Sample
    randomly within the cohort instead.
12. **Junk hubs are a small cleanup.** Only **7 of 1,346** match an
    imperative-task prefix (Locate/Extract/Explore/…), so this needs
    tagging-and-excluding, not a policy debate. But research-note hubs
    (fi201713: "Need to verify if these values are…") escape that regex — a
    NO-CLAIM verdict on a *non-control* hub must route to junk-triage,
    never be stamped decomposed.
13. **`because` ≠ `conjunct-of`.** fi176422/fi176399 flatten
    causal/contrastive structure into peer conjuncts; the relation
    vocabulary cannot express "Y is the mechanism for X". Decide before
    apply — apply writes links phase-3 reviewers read, and unwinding is
    expensive.

## Sequencing

P0-1..4 + P1-5..8 are small and independent: one cycle, re-run the same 25
(stable verdicts make it a clean A/B), then ~100 unseen hubs to confirm the
gates, then the full 1,346. Do **not** run the 1.3k on the current scorer —
it stamps ~19% of the population as atomic without reading the sentence that
matters.

The labelled 25 (verdict class per hub, above) should become a regression
fixture alongside the packaged
`src/precis/data/taproot/extraction_passages.jsonl`, which also gives the
un-evaluated `qualify_claim` step (`taproot-directed-claim-minting.md`)
its eval harness.
