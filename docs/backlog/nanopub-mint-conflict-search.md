---
status: draft
title: mint-time conflict search — every new nanopub hunts its own opposition before the claim freezes
blocked-by: disputes-edge-nonblocking-disagreement
---

# Mint-time conflict search

Reto, 2026-09-02: *"when we make a new nanopub, llm should search for
conflicting info and either incorporate in nanopub or also write that down
as nanopub and link it."*

## Motivation / why

Conflict detection today is retrospective and incidental: `hub_refine`
stumbles on a `contradicts` verdict while widening evidence, and the
disputes spec's "first run" is a corpus-wide sweep. Nothing looks for
opposition at the moment it matters most — **before the claim string
freezes at approve** (`nanopub/mint.py::approve`), while the wording can
still absorb a caveat and the reviewer is already paying attention. This
is the mint-time trigger the disputes spec
(`docs/backlog/disputes-edge-nonblocking-disagreement.md`) deliberately
left out: that item builds the vocabulary (`disputes`, non-blocking) and
the adjudication tier; this item makes every new nanopub *use* it. It is
also the "epistemically flat" fix applied at the door instead of by sweep
— new claims enter already knowing their neighbours disagree.

## In scope

A conflict-search pass over the corpus for the candidate claim, run on the
mint path, with three outcomes mirroring Reto's request:

1. **Search.** Reuse `hub_refine`'s machinery: ANN over paper/patent body
   chunks + claim-hub embeddings for the candidate sentence, then a
   BIG-tier LLM verify per candidate with the existing
   `yes/partial/no/contradicts` verdict shape. Add one retrieval trick the
   disputes spec's floor-caveat motivates: also embed 1–3 LLM-generated
   *negated/opposing paraphrases* of the claim and search those — "X has
   no effect on Y" sits far from "X enhances Y" in embedding space, so
   searching only the claim's own phrasing is structurally blind to the
   disagreements most worth finding.
2. **Incorporate.** Conflicts surface to the reviewer at approve time as
   an advisory panel (candidate passage + verdict + reasoning). If the
   conflict is a scope/caveat on the same claim (the disputes spec's
   expected-majority `scope-mismatch`), the fix is wording: the reviewer
   amends the claim before it freezes. No edge needed.
3. **Counter-nanopub + link.** If the conflicting info is a genuine
   opposing claim with its own grounding in a held source, mint it as its
   own hub through the normal directed-mint path (own sentence, own
   grounding passage) and file a `disputes` edge between the two. Both
   remain mintable — `disputes` is non-blocking by design; adjudication
   comes later via the disputes spec's Part 2.
4. **Honest negative.** "No conflict found" is a statement, not silence:
   record searched-at + candidate count on the `nanopub_publish` row
   (meta), so "no known conflict as of <date>" is checkable and a later
   sweep knows what was already covered.

Edge writes must tolerate re-runs: the DB now enforces uniqueness
(`links_endpoints_relation_idx`, since 278624b6 — the disputes spec's
"no unique constraint" hazard note is stale), so a re-run of approve must
upsert/skip cleanly rather than error on the duplicate.

## Explicitly NOT in scope

- **Blocking.** No conflict-search verdict ever hard-blocks a mint. The
  disputes spec establishes why: an unreviewed LLM suspicion raises a
  question (`disputes`), it does not veto (`contradicts` stays
  adjudication-derived).
- **Adjudication** — Part 2 of the disputes spec, untouched here.
- **External search** (semanticscholar / websearch / perplexity). A
  counter-hub must ground in a held passage; an external hit routes
  through paper acquisition first, and a stub is not citable
  (`docs/backlog/paper-acquisition-s2-gap` territory). Corpus-only in
  slice 1; external hunt is a named follow-on.
- **Retro sweep** over already-approved/published nanopubs — same worker,
  separate run, belongs to the disputes spec's "first run" plan.

## Acceptance criteria

- Approving a candidate whose corpus contains a planted opposing claim
  surfaces that claim to the reviewer before the string freezes, with the
  passage and verdict shown.
- Reviewer can proceed on any verdict — approve is never mechanically
  blocked by this pass.
- A confirmed opposing claim can be minted as its own hub and linked with
  `disputes` in the same review sitting; re-running approve does not
  duplicate the edge.
- A candidate with no conflicts records searched-at + candidates-checked
  on its `nanopub_publish` meta.
- Negated-paraphrase retrieval demonstrably retrieves at least one
  opposition pair that same-phrasing ANN misses (test with the disputes
  spec's seed pair fi191120 vs fi218681, or a synthetic fixture).

## Target + blast radius

- `nanopub/mint.py::approve` — the hook point (advisory, pre-freeze).
- New ref-pass worker (or `hub_refine` sibling) in `src/precis/workers/`,
  registered in `workers/registry.py`, LLM via the router seam.
- `links` write door for `disputes` — whichever door the disputes spec's
  Part 1 item 3 picks; this item consumes it, doesn't build it.
- `precis_web` approve form — the advisory conflict panel.
- Skills: `precis-nanopub-help` gains the mint-time-conflict step.
- Gates (`nanopub/gates.py`) untouched.

## Open questions / decisions log

- **Where does the search run** — synchronously inside interactive
  approve (latency: N candidates × BIG-tier verify), or as a standing
  worker pass over `candidate`-state hubs so results are already waiting
  when the reviewer arrives? Leaning worker-pass with an approve-time
  freshness check; decide before `ready`.
- Does the searched-at negative statement ride into the *published*
  provenance graph, or stay a local honesty record? (Publishing "searched
  and found none" is close to the negative-results pathway in
  `claim-publication-nanopub-ots.md` — possibly the same artifact shape.)
- Is negated-paraphrase retrieval slice 1 or a fast-follow? It's the part
  that beats the retrieval floor, but it multiplies search cost ×(1+k).
