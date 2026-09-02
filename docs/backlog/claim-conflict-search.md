---
status: draft
title: claim conflict search — every claim hunts its own opposition, at mint and retroactively, with coverage tracked
blocked-by: disputes-edge-nonblocking-disagreement
---

# Claim conflict search — at mint, and retroactively, with a coverage ledger

Reto, 2026-09-02: *"when we make a new nanopub, llm should search for
conflicting info and either incorporate in nanopub or also write that down
as nanopub and link it."* Extended same day: run it **when we make a
claim** (not only at nanopub approve), run it **retroactively on existing
claims and keep track that we did**, and rank candidate papers by
trustworthiness **without rejecting hidden small voices entirely** (that
last piece: `docs/backlog/source-trust-prior.md`).

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

## In scope

**One mechanism, two populations.** A standing ref-pass worker (a
`hub_refine` sibling in `src/precis/workers/`, registered in
`workers/registry.py`) sweeps every claim hub whose
`meta.conflict_search.version` is missing or older than the current
search version. That single watermark rule makes "new claim gets swept
promptly after mint" and "retro backfill of the existing corpus" the same
code path — no separate backfill tool, and bumping the version re-sweeps
everyone after a method change.

1. **Search.** Reuse `hub_refine`'s machinery: ANN over paper/patent body
   chunks + claim-hub embeddings for the claim sentence, then a BIG-tier
   LLM verify per candidate with the existing
   `yes/partial/no/contradicts` verdict shape. Add one retrieval trick
   the disputes spec's floor-caveat motivates: also embed 1–3
   LLM-generated *negated/opposing paraphrases* of the claim and search
   those — "X has no effect on Y" sits far from "X enhances Y" in
   embedding space, so searching only the claim's own phrasing is
   structurally blind to the disagreements most worth finding.
2. **Budgeted verify, trust-ordered, with a small-voice floor.** LLM
   verification is the expensive step, so candidates are ranked before
   spending: once `source-trust-prior` lands, order by trust — but
   reserve a fixed fraction of every hub's verify budget for
   below-median-trust candidates, and never drop a candidate on trust
   alone. Dissent disproportionately lives in low-prestige venues;
   a pure trust ordering would re-flatten exactly what this pass exists
   to find. Until the prior lands, order by cosine distance.
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
   grounding passage) and file a `disputes` edge between the two. Both
   remain mintable — `disputes` is non-blocking by design; adjudication
   comes later via the disputes spec's Part 2.

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
- **Trust scoring itself** — signals, score shape, storage are
  `docs/backlog/source-trust-prior.md`; this item only consumes the
  ordering and owns the floor rule.
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
- A confirmed opposing claim can be minted as its own hub and linked with
  `disputes` in the same review sitting; re-sweeping does not duplicate
  the edge.
- Negated-paraphrase retrieval demonstrably retrieves at least one
  opposition pair that same-phrasing ANN misses (seed pair fi191120 vs
  fi218681, or a synthetic fixture).
- With a trust prior present, a below-median-trust candidate still
  receives verify spend on a hub whose high-trust candidates would
  exhaust the budget (floor rule test, fixture-based).

## Target + blast radius

- New ref-pass worker in `src/precis/workers/`, registered in
  `workers/registry.py`, LLM via the router seam; hub
  `meta.conflict_search` writes.
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
- Does the searched-at negative statement ride into the *published*
  provenance graph, or stay a local honesty record? (Close to the
  negative-results pathway in `claim-publication-nanopub-ots.md` —
  possibly the same artifact shape.)
- Is negated-paraphrase retrieval slice 1 or a fast-follow? It's the part
  that beats the retrieval floor, but it multiplies search cost ×(1+k).
- Verify-budget size per hub, and the floor fraction (starting point:
  ~20% of spend reserved for below-median trust) — tune on the backfill's
  first dense neighbourhood.
