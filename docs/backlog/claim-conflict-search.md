---
status: ready
title: claim conflict search — every claim hunts its own opposition, at mint and retroactively, with coverage tracked
model: opus
blocked-by: disputes-edge-nonblocking-disagreement
---

# Claim conflict search — at mint, and retroactively, with a coverage ledger

Reto, 2026-09-02: *"when we make a new nanopub, llm should search for
conflicting info and either incorporate in nanopub or also write that down
as nanopub and link it."* Extended same day: run it **when we make a
claim** (not only at nanopub approve), run it **retroactively on existing
claims and keep track that we did**, and rank candidate papers by
trustworthiness without rejecting hidden small voices — using the
**existing** `paper_rank` score (`workers/paper_rank.py`,
`refs.meta.paper_rank.read_first`), not a new one.

## Motivation / why

Conflict detection today is retrospective and incidental: `hub_refine`
stumbles on a `contradicts` verdict while widening evidence. Nothing
systematically asks, for each claim, "who disagrees?" — which is the
"epistemically flat" gap the disputes spec
(`docs/backlog/disputes-edge-nonblocking-disagreement.md`) names. That
item builds the vocabulary (`disputes`, non-blocking) and the adjudication
tier; this item makes every claim *use* it: new claims are swept shortly
after mint, the existing corpus is backfilled by the same pass, and the
nanopub approve step surfaces the results while the claim wording can
still absorb a caveat.

**Blocked-by scope note:** only the edge-filing half (item 5, and item 4's
"linkable dispute" rendering) needs Part 1's `disputes` relation. Items
1–3 (search, budgeted verify, coverage ledger) are independently buildable
and can start accumulating `meta.conflict_search` coverage before Part 1
lands; verdicts wait in the ledger until there's an edge to file.

## In scope

**One mechanism, two populations.** A standing ref-pass worker (a new
worker in `src/precis/workers/`, registered in `workers/registry.py`)
sweeps every claim hub whose `meta.conflict_search.version` is missing or
older than the current search version. That single watermark rule makes
"new claim gets swept promptly after mint" and "retro backfill of the
existing corpus" the same code path — no separate backfill tool, and
bumping the version re-sweeps everyone after a method change.

1. **Search.** ANN over paper/patent body chunks + claim-hub embeddings
   for the claim sentence, **plus** 1–3 LLM-generated *negated/opposing
   paraphrases* of the claim embedded and searched the same way — "X has
   no effect on Y" sits far from "X enhances Y" in embedding space, so
   searching only the claim's own phrasing is structurally blind to the
   disagreements most worth finding. Slice 1, decided (see log).
   **Boundary with `hub_refine`:** re-derive the discovery wiring in the
   new pass per the codebase's cross-task-seam precedent — do NOT import
   `hub_refine`'s underscore-private helpers (`_citation_candidates`,
   `_Candidate`, …; they're excluded from its `__all__` deliberately).
   The sanctioned shared seam is `workers/_chase_llm.py`: the LLM verify
   (yes/partial/no/contradicts verdict shape) is imported from there,
   extended if needed there, never forked.
2. **Budgeted verify, paper_rank-ordered, with a small-voice floor.** LLM
   verification is the expensive step, so candidates are ranked before
   spending, by the existing `meta.paper_rank.read_first` (five-signal
   deterministic score: fwci, corpus citation-graph PageRank, citation
   velocity, methodology + reproducibility markers; retracted capped at
   20 but never excluded — the score already ranks-without-rejecting by
   design). It's documented as reading-priority, not claim-quality —
   which is exactly the right shape for "whose conflict deserves verify
   spend first." The floor rule lives here: reserve a fixed fraction of
   every hub's verify budget for below-median-`read_first` candidates,
   and never drop a candidate on rank alone — dissent disproportionately
   lives in low-prestige venues, and a pure rank ordering would
   re-flatten exactly what this pass exists to find. Candidates whose
   paper lacks `paper_rank` order by cosine distance within their band.
3. **Coverage ledger.** Each swept hub records
   `meta.conflict_search = {version, at, candidates_checked,
   disputes_filed}`. This is the "keep track that we did it": "no known
   conflict as of <date>, method <version>" is a checkable statement, not
   silence; coverage (swept/total per version) is one query; the nanopub
   view and approve form read it. Lives on the hub, not
   `nanopub_publish` — most claims never reach a mint attempt.
4. **Incorporate.** At nanopub approve (`nanopub/mint.py::approve`), the
   stored results surface as an advisory panel (candidate passage +
   verdict + reasoning), with a freshness check (stale version or old
   `at` → re-sweep before freeze). If the conflict is a scope/caveat on
   the same claim (the disputes spec's expected-majority
   `scope-mismatch`), the fix is wording: amend the claim before it
   freezes. No edge needed.
5. **Counter-claim + link.** If the conflicting info is a genuine
   opposing claim with its own grounding in a held source, mint it as its
   own hub through the normal directed-mint path (own sentence, own
   grounding passage) and file a `disputes` edge between the two. This
   explicit edge-file is a **deliberate backstop**, not redundancy: once
   Part 1 repoints `place()`, directed-mint's own `block()` →
   `dedup_judge()` → `place()` cascade may file the edge automatically —
   but this pass's negated-paraphrase candidate is often one `block()`'s
   narrower same-phrasing search would never surface, so the cascade
   cannot be relied on. The acceptance test asserts the edge *exists*
   after the sitting, whichever path wrote it. Both hubs remain mintable
   — `disputes` is non-blocking by design; adjudication comes later via
   the disputes spec's Part 2.

Edge writes must tolerate re-runs: the DB enforces uniqueness
(`links_endpoints_relation_idx`), so a re-sweep must upsert/skip cleanly
rather than error on the duplicate.

**Backfill order:** dense topic neighbourhoods first (MOF conduction, DNA
bricks, molecular switches), per the disputes spec's "first run" —
conflicts hide where coverage is thickest.

## Explicitly NOT in scope

- **Blocking.** No conflict-search verdict ever hard-blocks a mint. An
  unreviewed LLM suspicion raises a question (`disputes`), it does not
  veto (`contradicts` stays adjudication-derived).
- **Adjudication** — Part 2 of the disputes spec, untouched here.
- **Changing `paper_rank`** — this item consumes
  `meta.paper_rank.read_first` as-is; new signals or reweighting belong
  to that worker's own follow-ons.
- **External search** (semanticscholar / websearch / perplexity). A
  counter-hub must ground in a held passage; an external hit routes
  through paper acquisition first, and a stub is not citable. Corpus-only
  in slice 1; external hunt is a named follow-on.

## Acceptance criteria

- A newly minted claim hub is swept by the worker pass without any
  nanopub intent, and carries `meta.conflict_search` afterwards.
- Backfill is the same pass: on first deploy the worker walks existing
  hubs; coverage (swept/total at current version) is answerable with one
  query; bumping the search version marks everyone stale.
- Approving a candidate whose corpus contains a planted opposing claim
  shows that claim to the reviewer before the string freezes, with the
  passage and verdict; approve is never mechanically blocked.
- After a review sitting that confirms a genuine opposing claim, the
  counter-hub exists and a `disputes` edge joins the pair (written by
  this pass or by the `place()` cascade — the test asserts existence,
  not authorship); re-sweeping does not duplicate it.
- Negated-paraphrase retrieval demonstrably retrieves at least one
  opposition pair that same-phrasing ANN misses (seed pair fi191120 vs
  fi218681, or a synthetic fixture).
- A below-median-`paper_rank` candidate still receives verify spend on a
  hub whose high-rank candidates would exhaust the budget (floor rule
  test, fixture-based).

## Target + blast radius

- New ref-pass worker in `src/precis/workers/`, registered in
  `workers/registry.py`; LLM verify via `workers/_chase_llm.py` + the
  router seam; hub `meta.conflict_search` writes;
  `meta.paper_rank.read_first` reads.
- `nanopub/mint.py::approve` — advisory surface + freshness check.
- `links` write door for `disputes` — whichever door the disputes spec's
  Part 1 item 3 picks; this item consumes it, doesn't build it.
- `precis_web` approve form — the advisory conflict panel.
- Skills: `precis-nanopub-help` + `precis-taproot-help` gain the
  conflict-search step and the coverage-ledger read.
- Gates (`nanopub/gates.py`) untouched.

## Open questions / decisions log

- **Decided (2026-09-02, Reto):** trigger is claim-mint + retro backfill
  via one watermarked worker pass, not approve-only; coverage must be
  tracked; trust ranks but never rejects.
- **Decided (2026-09-02, Reto):** trust ordering uses the existing
  `paper_rank` score — no new trust/goodness score (a drafted
  `source-trust-prior` item was deleted same day as duplicate).
- **Decided (2026-09-02, post ready-review):** negated-paraphrase
  retrieval is slice 1 — it is the part that beats the retrieval floor;
  without it the pass mostly re-finds paraphrase neighbours, which is
  dedup's job, not this pass's. Cost bounded by k≤3 paraphrases.
- **Decided (2026-09-02, post ready-review):** no imports of
  `hub_refine` privates; re-derive discovery, share only via
  `workers/_chase_llm.py` (see In-scope item 1).
- **Decided (2026-09-03):** Part 1 shipped AND deployed (0de3ec7c,
  migration 0151), so the blocked-by contingency is moot — slice 1
  (items 1–3) files `disputes` edges directly on confirmed contradicts
  verdicts instead of parking verdicts in the ledger. Items 4–5
  (approve advisory panel, counter-claim mint) remain follow-on slices.
- **SHIPPED (2026-09-03): slice 1** — `workers/conflict_search.py`
  (items 1–3 + direct `disputes` filing): watermarked claim-lease pass,
  negated-paraphrase ANN (finding-kind candidates re-derive the
  canonical-hub predicate — scratch chase findings never file), 20%
  small-voice floor over `paper_rank.read_first`, shared `_chase_llm`
  verify, `meta.conflict_search` ledger. Dark: enable via a
  `service_config` row (`conflict_search`). REMAINING in this item:
  item 4 (approve-time advisory panel + freshness re-sweep), item 5
  (counter-claim mint through directed mint), backfill ordering /
  budget tuning on the first dense neighbourhood, and the two open
  questions below.
- Does the searched-at negative statement ride into the *published*
  provenance graph, or stay a local honesty record? (Close to the
  negative-results pathway in `claim-publication-nanopub-ots.md` —
  possibly the same artifact shape.)
- Verify-budget size per hub, and the floor fraction (starting point:
  ~20% of spend reserved for below-median rank) — tune on the backfill's
  first dense neighbourhood.
