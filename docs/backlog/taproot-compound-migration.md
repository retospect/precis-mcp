---
status: draft
title: Taproot compound→atomic migration — corpus-wide apply, blocked on regrounding
model: opus
blocked-by: fisheye-conjunct-of-surfacing
---

# Taproot compound→atomic migration

Decomposition machinery shipped 2026-08-13: `extract_claim` returns a
`ClaimExtraction` (atoms + optional compound + not_claims), `conjunct-of`
relation (migration 0126) through `link_claims`, `hub.apply_extraction`
orchestrator, compound hubs hold no direct evidence, compound trust =
worst-of its atoms, workers exclude compounds from refine/re-embed, backfill
runs the cascade per atom + compound. Present-state truth:
`src/precis/taproot/__init__.py`. What remains: migrate the **existing**
1,346-hub corpus, run as a quiet-window operation — blocked on the atom
regrounding prerequisite below.

## Population (probed prod 2026-08-14)

1,346 live claim hubs (the earlier ~112 figure was the reground review set,
a different denominator).

- Evidence: 79% have ≥1 evidence edge, overwhelmingly exactly 1 (935 of
  1,065); 281 have none — re-pointing is mostly a one-edge decision per hub.
- Inbound cites: 81% cited from prose, mostly exactly 1 (max 6). **Cites do
  NOT need re-pointing** — prose cites the bundling sentence, which stays
  the compound hub.
- Compoundness proxy on titles: 30% contain " and ", 39% are >160 chars,
  18% both, 3% contain ";". Likely-compound band ≈ 250–530 hubs.
- Claim-links: zero `refines`, zero `conjunct-of` — clean slate.
- Minting rate ~175/month (July spike: 942) — the population grows while
  the migration runs; the process must be resumable, not a one-shot
  snapshot.

## Strategy — phases 0–3 (decided 2026-08-14)

**Pre-reqs:** (1) deploy the machinery (on `main` only — post-deploy, new
backfill-minted claims arrive already decomposed; `chase.py`'s bridge
deliberately does not decompose, per its docstring). (2)
`fisheye-conjunct-of-surfacing.md` — the human review surface can't show
atom↔compound structure yet, blocking for phase 3.

- **Phase 0 — score and cohort** (read-only). Rank hubs by compoundness:
  title heuristics (conjunctions, length, semicolons) + source-chunk section
  (intro/abstract/conclusion ranks high, results low). Emit likely-compound
  / uncertain / likely-atomic cohorts. **Score the body claim sentence
  (`finding_body` ord=0 chunk), never `refs.title`** — they differ on
  572/1,346 hubs, 259 (19%) score atomic by title but compound by body.
- **Phase 1 — dry-run decomposition** (read-only, LLM spend only). Run
  extraction over every hub's claim sentence, gated verdicts
  (`split`/`pass-through`/`lossy`/`nested`/`no-claim`/`error`; `lossy`/
  `nested` held for review, never applied). ~1.3k calls.
- **Phase 2 — apply, atomically per hub** (the quiet window). Per hub in one
  transaction: mint atom hubs (converge via the normal block→judge→place
  cascade), link `conjunct-of`, re-point evidence edges compound→atom, stamp
  `meta.taproot_decomposed_at` so re-runs skip it. Idempotent by
  construction. Evidence re-point is the one judgment call per hub — never
  blanket-copy an edge to every atom; verify per-atom (hub_refine-style),
  attach where verified, file `needs_review` when nothing verifies. The
  re-point implements the add-first invariant in deterministic code (adds
  first with `links` read-back, prunes only for confirmed adds,
  post-transaction `count(live edges) > 0` re-check, partial-failure counts
  in the result, intent-vs-committed diff as the repair mode) — the same
  contract as `hub_refine`'s (`docs/backlog/taproot-reground.md`'s
  "Applier must enforce add-first in code" section); one code path, not two.
- **Phase 3 — human review, triaged not exhaustive.** At 1,346 hubs a full
  pass isn't plausible: review only `needs_review` placements, low-confidence
  splits, and a ~5% random QA sample of auto-applied hubs. Fisheye (pre-req
  2) is the surface.

**Quiet window** = phase 2 only: pause `hub_refine`/`chase_trigger` for the
window so nothing refines/re-embeds mid-repoint; avoid 02:00–03:30 UTC
(nightly backup + caspar's daily reboot). Phases 0/1/3 need no window.

**Rollback posture:** per-hub atomic; minted atoms are ordinary refs
(tombstone/undelete exists), `conjunct-of` edges deletable, the stamp
records what was touched — reverted hub-by-hub, not by restore.

Related, not blocking: atom hubs mint with `scope` in their dedup identity
but hubs have no scope write door after mint
(`taproot-hub-scope-no-edit-door.md`) — any apply-time scope mistake is
currently uncorrectable through the product surface.

## Quality gates — all 13 shipped

`taproot/migrate.py` (P0/P2: `lossy`/`nested` verdicts, body-sentence
scoring, `--json` persist, seeded random controls, `junk_candidate`,
`escalate_fn`) + `taproot/canon.py` (P1/P2-13: enumerate-then-emit, modality
+ mechanism rules, compound synthesis, scope validation,
`extract_claim_strict_big`/`_strict_haiku`). Calibration fixture:
`tests/fixtures/taproot/migration_pilot_25.jsonl` (22/25 exact, 3 documented
xfail). haiku is the primary extractor (`extract_claim_strict_haiku`,
selective BIG escalation for `lossy`/`nested`/`no-claim` only — not a
blanket tier bump); a 100-hub run against the deployed gates re-classified
clean (49 split / 1 pass-through / 6 no-claim / 23 lossy / 15 nested / 6
error — the 6 errors are persistent local `claude -p` exit-1s, re-run or
investigate if the rate holds at scale).

**Open:**

- Run the full 1,346-hub dry-run (canary + 100-hub subset are green; the
  full run has not executed).
- **Decide: does `because` express `conjunct-of`?** fi176422/fi176399
  flatten causal/contrastive structure ("Y is the mechanism for X") into
  peer conjuncts — the relation vocabulary can't express it. Decide before
  apply; apply writes links phase-3 reviewers read, and unwinding is
  expensive.
- **haiku-lane blind spot:** `extract_claim_strict_haiku` is a documented
  router-bypass (`call_claude_p` + `resolve_model(Tier.MEDIUM)`, listed in
  `EXCLUDED_OPERATIONS`) riding the OAuth subscription — it writes no
  `llm_call_log` rows, so this lane is invisible to the budget breaker.
  Accepted (fix_gripe precedent); per-call `max_usd` cap + debug cost log
  only.

## Apply prerequisite — atom re-grounding (blocking)

**No source, no atom** (Reto, 2026-08-15, binding): every extracted atom
must be re-grounded against actual paper text — its own quote + locator
snip — before placement. Atoms extracted from the hub sentence alone can
invent content present in no shown source (fi34985's ~0.1 eV benchmark,
fi176551's "4 binding domains"). Core pass IMPLEMENTED
(`taproot/reground.py`: paper collection both provenance shapes, hearsay-
section exclusion, overlap-ranked passages, batched verify, quote
fold+substring+uniqueness validation; `taproot-migrate reground` CLI stage;
apply withholding via `atoms_withheld_ungrounded`). Not the nanopub mint
gate — that (sibling session) owns Layer A validation at mint time; this
pass produces the record the gate consumes.

**Blocking, not yet apply-grade:**

- **Grounded rate ~20%** on repeat runs (30/158 then 33/166) — dominant
  failure is verify-reply flakiness (a degraded MEDIUM-lane rung
  wholesale-rejecting a hub's whole atom batch; an isolated repro of the
  same inputs flips to 3/3 supported), not verifier strictness. **Gate on
  lane health before the next attempt** — use T0d's hourly rollups to
  confirm the MEDIUM lane is serving cleanly.
- **Embedding-ranking fix — named blocking twice.** The lexical ranker
  drowns on large papers: papers >150 chunks ground 0/15 atoms (a 571-chunk
  review misses textbook-support atoms outright). Both the 2026-08-15
  calibration run and the attempt-4 re-run name this as the other blocking
  fix alongside lane health.
- Distinct reason `quote-validation-failed` vs `verify-rejected` (shipped) —
  keeps flakes visible instead of folding into "unsupported".
- Non-contiguous model quotes (stitching two spans) fail `_validate_quote`
  and are then indistinguishable from model-said-unsupported — verify
  prompt needs a single-contiguous-span constraint.

Extraction-contract riders once regrounding lands: **bound semantics**
(scope quantity values carry `bound: exact | upper | lower | approx`, read
off the quote, not the fi sentence) and **causal qualifiers stay in the
claim** (fi34850's atom must keep "because of zero bandgap", not the
blanket version — extends the modality/mechanism gate).

Cost shape: 49 dry-run splits ≈ 150 atoms; full run ~1.3k hubs → order
4–5k atom-verify calls on the haiku lane (~$0.05/call). Bound the
passage-candidate count per atom (top-k) and batch per hub before scaling.

## Explicitly NOT in scope

- A second decomposition mechanism — every line lands in the existing
  `hub_refine`/`migrate`/`reground`/`apply_migrate` spine.
- Re-pointing inbound prose cites — they stay on the compound hub.
- Resolving whether `chase.py::_taproot_bridge` should decompose
  post-migration, or chase-minted hubs simply queue for a standing version
  of phase 0 (monthly re-score) — open, not decided.

## Acceptance criteria

- Full 1,346-hub dry-run completes with persisted JSONL outcomes, gated
  verdicts, zero blanket-BIG spend.
- Every `split`-verdict hub's atoms are regrounded (source quote + locator
  snip) or withheld `needs_review`/hanging before phase 2 touches it.
- Phase 2 apply is idempotent (`meta.taproot_decomposed_at` stamp) and its
  evidence re-point never leaves a hub at zero live edges.
- Phase 3 review surface (fisheye) shows atom↔compound structure before any
  human sign-off pass runs.

## Target + blast radius

`src/precis/taproot/{migrate,canon,reground,apply_migrate,hub}.py`,
`src/precis/cli/taproot_migrate.py`, `conjunct-of` links (migration 0126),
`meta.taproot_decomposed_at` stamp, `hub_refine`/`chase_trigger` (paused
during the quiet window only).

## Open questions / decisions log

- **DECIDED (Reto, 2026-08-14):** phase-3 review is triaged-only
  (`needs_review` + low-confidence + ~5% QA sample).
- **DECIDED (Reto, 2026-08-14):** evidence re-point = LLM per-atom verify.
  Human sign-off stays available on top, not a migration gate.
- **DECIDED (Reto, 2026-08-14):** run now — deploy, then phases 0/1
  immediately, phase 2 in the next quiet window (avoid 02:00–03:30 UTC).
- **DECIDED (Reto, 2026-08-15, binding):** no source, no atom — every atom
  re-grounded before placement.
- **Open:** `because` ≠ `conjunct-of` — decide the relation before apply
  (see Quality gates above).
- **Open:** whether `chase.py::_taproot_bridge` should decompose
  post-migration vs. chase-minted hubs queuing for a standing monthly
  phase-0 re-score.
