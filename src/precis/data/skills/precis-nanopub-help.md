---
id: precis-nanopub-help
title: precis — publishing claims as signed nanopubs (view='nanopub' + mint pipeline)
summary: get(kind='finding', view='nanopub') renders a claim hub as TriG — a draft pre-mint, the exact signed bytes post-mint; the mint pipeline (approve → sign → OTS anchor) is CLI/human-driven, not an agent verb
applies-to: get (kind='finding', view='nanopub'); precis nanopub CLI (human-run); reading publish state
status: active
---

# precis-nanopub-help — the published identity of a taproot claim

A reviewed taproot claim can be **minted** as a nanopublication: a
signed, content-addressed TriG artifact (trusty URI under
`https://w3id.org/np/`) whose provenance carries only universal anchors
— DOI, `pdf_sha256` of the exact quoted copy, a verbatim quote, and a
normalized `searchSnip` that locates the passage in any copy. Taproot
stays authoritative; the nanopub is the frozen published form.

## What an agent can do

- `get(kind='finding', id='fi<id>', view='nanopub')` — the TriG
  rendering. Pre-mint you get an **unsigned draft** (placeholder URI,
  `#` status comments; missing grounding is flagged, not invented).
  Once signed you get the **exact frozen bytes** plus a comment header
  naming the trusty URI and the `nanopub_artifacts` row that holds the
  authoritative copy.
- Read the header's publish state: `candidate → reviewed → signed →
  anchored → published → superseded/retracted` (`rejected` off
  `reviewed`). A hub with a live `contradicts` edge renders an
  UNMINTABLE warning — disputed claims are visible internally,
  unpublishable externally, until adjudicated by artifacts.

## What an agent must NOT do

- **Minting, signing, anchoring, sign-off and publishing are not agent
  verbs.** `precis nanopub approve/sign/signoff/anchor/publish` is run
  by a person (the `/nanopub` web surface is the same interactive
  door); the attesting key is invocable only from those surfaces. A bot
  signature alone never publishes anything: publication requires an
  **attesting** entry in the trust allowlist, and the registry POST
  (`publish --live`) is the one irreversible step — CLI-only, never
  automated.
- Never edit a hub that shows `reviewed` or later state to "fix" its
  wording — the approved string is frozen; an edit flips the row back
  for re-review (pre-publication) or forces a public supersede
  (post-publication). Propose the change instead.

## Mint gates (why a claim you drafted may not mint)

Layer-A validators run at approve and again at sign; the common
failures an extraction agent can avoid up front:

- **Primary sources only** — grounding whose chunk lives in a
  references list / related-work / prior-art / background section is
  hearsay and rejected, even when the quote checks out. Cite the paper
  that DID the work; if only a secondhand mention is held, the claim
  stays *hanging* (mintable, unpublishable) while the original is
  hunted.
- **No source, no atom** — every claim needs its own verbatim quote
  plus a snip that matches uniquely in the paper's stored text.
- **Quantities carry bound semantics** — `exact` / `upper` / `lower` /
  `approx-range`; "up to 400:1" and "400:1" are different claims.
- **Structured fields must be quote-contained** — a
  material/method/quantity value the quotes don't state is an
  overclaim.
- **Compounds are derivations** — conjunct-of atoms minted first; the
  compound cites atoms, never papers. Cross-binding (a property from
  system B on a phenomenon from system A) is new content: it needs its
  own evidence or mints as a `precis:Hypothesis` (declarative sentence,
  type carries the epistemic status, `testableBy` names the
  discriminating experiment).

## Publish-time gates (past mint — why a signed claim may not publish)

Distinct from mint gates; enumerated by `precis nanopub preflight` and
on the `/nanopub/fi<id>` review page:

- **Withheld evidence edges** — an evidence edge neither
  verified-by-refine nor human-signed-off blocks publication; there is
  no mute button. Verification (the refine chase) is the agent-side
  remedy; the literal sign-off is human-only.
- **Trust allowlist** — only pinned (identity, key-fingerprint) pairs
  are trusted, and publishing requires the *attesting* (human) entry.
- **Order** — atoms publish before the compounds citing them; hanging
  claims never publish; a drifted or disputed hub is blocked.

## Registry mirror (read-only sidecar)

A local cache of *other people's* published nanopubs — dark behind
`PRECIS_MIRROR_ENABLED`; external nanopubs never enter taproot as
evidence. `precis nanopub mirror status` shows counts;
`sync --live [--all]` pulls missing codes (read-only GETs). External
artifacts are frozen by construction (the code IS the content hash);
`verified` means the trusty recompute over the fetched bytes matched.
Retraction/supersede are *flags derived from edges* (only a same-signer,
verified retraction counts), never exclusions. Concurrence — an external
nanopub asserting one of our AIDA sentences — raises an `alert`.
