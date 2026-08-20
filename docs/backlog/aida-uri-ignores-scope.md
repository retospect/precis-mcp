---
status: draft
title: "scope-forked hubs mint the same AIDA URI — the collision is a symptom of an inadmissible sentence"
---

# Two distinct claims, one published identity

`nanopub/aida.py::aida_uri` keys on the canonicalized **sentence alone**.
`identity.py::make_taproot_hub_paper_id` keys on the sentence **plus the
`scope` object**. So two hubs deliberately forked on scope are distinct
inside precis (different `pub_id`, different hub) and identical at
publication (same AIDA URI).

Measured read-only against prod 2026-08-20 — every live `TAPROOT:claim` hub
sharing a normalized sentence with another:

| sentence | hubs | distinct scopes |
|---|---|---|
| "carbon nanobud material has been engineered into printed touch sensors" | 2 | 2 |
| "periodic graphene nanobuds exhibit Pt binding energies of −3.34 and −3.78 eV …" | 2 | 2 |

Two pairs, corpus-wide — the same two already tracked as duplicate pairs
(`fi191179`/`fi191260`, `fi191192`/`fi191262`). Both are nanobud hubs in the
cohort awaiting re-approval, so this is live for the campaign, not
theoretical.

## `aida_uri` is probably not the thing to fix

AIDA is *An Atomic Descriptive sentence* — keying on sentence text is the
standard's design, not our bug. Widening it to include scope would mint URIs
no other AIDA consumer could resolve, and would defeat the convergence
property the whole scheme exists for (two agents asserting the same sentence
land on the same node).

The collision is better read as a **symptom**: if a hub's `scope` changes what
the claim means, and that scope is not in the sentence, then the sentence is
not self-contained — and *self-contained* is an admissibility criterion the
claim was supposed to have passed. A sentence needing "…but only at 300 K" to
be true should say so. Scope is for retrieval facets and provenance, not for
carrying a load-bearing qualifier out of the assertion.

## What this decides for the dedup pass

The two pairs resolve one of two ways, and the AIDA collision is the tell:

- **They are the same claim** → merge them (the dedup pass's job); the
  collision disappears with the duplicate.
- **They are genuinely different claims** → their sentences are inadmissible
  as written. Reword each to carry its own qualifier, which changes the
  sentence, which changes `pub_id` — so this must happen **while the hubs are
  `candidate`**, before re-approval, same ordering constraint the notation
  and scope backfills already carry.

Either path is fine; publishing both as-is is not. Two nanopubs asserting
byte-identical sentences under one AIDA URI while claiming to be distinct
claims is exactly the "impeccably traced but epistemically flat" failure the
external review named.

## Check while you are in there

The 156 hubs carrying prose `scope` values (`claim-review-mechanism.md`) are
the population most likely to hide more of these — a prose scope is far more
likely to be load-bearing than a structured facet. The query above only finds
collisions that *already* share a sentence; a load-bearing scope on a hub with
a unique sentence is the same defect, undetected. Consider a lint: flag any
hub whose `scope` contains a quantity, condition or qualifier absent from its
sentence.

Found 2026-08-20 during the corpus-streamlining pass, while correcting the
identity model in `claim-publication-nanopub-ots.md`.
