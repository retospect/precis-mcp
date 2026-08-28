---
status: draft
title: Claim publication — signed nanopubs + OpenTimestamps anchoring, minted locally, pushed at publication time
model: opus
blocked-by: taproot-compound-migration
---

# Claim publication — nanopubs + OpenTimestamps

Built state (slices 1–5, registry mirror): `src/precis/nanopub/__init__.py`
package docstring — present-state home, not this file. This file is the
open-work residue.

Blocked on `taproot-compound-migration`, but per hub, not globally: a decomposed,
reviewed hub can publish while the rest of the graph is still compound.

## Patent / book (ISBN) grounding

Mint's grounding gate hard-requires a DOI per passage
(`nanopub/gates.py`: "no DOI — provenance content is DOI + quote + snip
(patent grounding is an open item)"). Same gap, two source types — design
together, one gate:

- **Patents.** The nanopub ecosystem is DOI-centric; no standard
  identifier fits. Likely a local scheme.
- **Books** (the Callister class). fi19981's best mid-span corroboration
  is Callister, *Fundamentals of Materials Science and Engineering* 3e
  (2008, Wiley, ISBN 978-0-470-12537-3) — "over one billion transistors …
  doubles about every 18 months" — no DOI, so the passage can't ride the
  artifact even though edge, quote, snip and pdf sha all check out.
  `urn:isbn:` (a registered URN namespace) in the provenance graph where
  DOI would go; edition matters — the sha pins the copy, the ISBN names
  the edition.

Gate shape for both: accept exactly one of doi | isbn | patent-no per
passage — quote verbatim-containment, snip uniqueness, pdf-sha pin, and
the hearsay checks are already identifier-agnostic. `_suggested_payload`
should emit an `isbn`/`patent-no` field when the ref has one and no DOI;
the approve form passes it through.

Until either lands: DOI-less passages stay out of minted payloads — keep
the corroborates edge internally, note the passage in the hub for the
human reader.

## Revocation and correction policy

Two unmade policy calls:

- **Allowlist revocation.** An identity leaving the allowlist after its
  claims were cited can't unpublish them; the remedy is a supersede or
  qualification nanopub recording the changed basis, but whether/when
  that fires is a judgement call, not decided.
- **Correction vs retraction vs invalidation.** `supersedes` /
  `invalidates` / `retracts` are all live vocabulary in the corpus; which
  applies when the *wording* was wrong vs the *claim* was wrong vs the
  *evidence* was misread is undecided.

## First registry POST + introduction nanopub

`nanopub/registry.py` is built and triple-gated (`interactive=True` +
`--live` + clean preflight); the first real POST is Reto's call, still
untaken. Two things want to exist first:

- **Introduction nanopub** — **built** (`src/precis/nanopub/intro.py`,
  `precis nanopub intro`): signs + (`--live`)
  publishes the key→ORCID declaration, records the trusty URI in the
  vault. The `approvesOf` path from an existing agent is still open
  (deferrable — rates agents, not claims); the out-of-band ORCID
  back-link (adding the trusty URI under 'Websites & social links') is
  still Reto's pending step, same as the first real registry POST above.
- **Fingerprint page** — `https://precis.retostamm.com/id/precis` must
  resolve to the public key fingerprints + validity windows, the only
  independent binding a reader has (`signedBy` alone proves nothing).
  Signing keys exist (`precis nanopub keygen` has run); publishing the
  page is Reto's pending step.

## Mirror pull

`nanopub/mirror.py` is built; `PRECIS_MIRROR_ENABLED` is now ON
cluster-wide (deploy `precis_worker_nanopub_mirror`), so the daily delta
sync runs. The initial ~87k-nanopub backfill is a separate one-time
manual door, still untaken: `precis nanopub mirror sync --live --all`
(`/nanopubs.json` returns the full code list in one flat array, no
paging — probed 2026-08-15).

## Three deferred publish-time gates

`nanopub/preflight.py`'s trust gate checks (signer, key) against an
open-window allowlist row today; three refinements are named-deferred in
its own docstring:

1. **Allowlist-as-published-artifact.** Version, sign, OTS-anchor the
   allowlist itself; each published claim records the version that gated
   it, so "only allowlisted signers were trusted" becomes third-party
   verifiable.
2. **Validity-window-vs-signature-time.** Check the window in force
   *when the signature was made*, not at preflight time; needs a
   trustworthy time source (the OTS anchor), so the two features are
   coupled.
3. **Inbound key-strength/DER/SPKI gauntlet.** Parse the DER, require
   valid SPKI, check modulus size, reject unknown algorithms — against
   *external* keys (our own already sign at 2048 minimum, 4096
   preferred, enforced at mint).

## Outbound retraction

Inbound detection is built (`ingest/provenance.py`: Crossref,
`refs.retraction_status`, `retracted-by`/`corrected-by`/
`concern-raised-by` links). Missing: the outbound half — emitting a
retraction/qualification nanopub for a published edge grounded in a
source that's since been retracted.

## Negative-results pathway

`workers/hub_refine.py` already computes verified non-support
(`meta.citation_misses`); publishing grounded negative checks
(`cito:disagreesWith`) has no artifact path yet. Plausibly higher value
than positives — the computation is already paid for, and almost nobody
in the ecosystem publishes this.

## Query-time section filter

The primary-source hearsay gate's mint-side check (`nanopub/gates.py`,
`section_path` matched against references/related-work/background
patterns) is plain SQL on a stored column. The *query-time* half —
filtering a search/hunt by `section_path` — doesn't exist: the column is
stored, shown in `view='toc'`, never filterable in search (migration
0118 dropped its dead index). Until built, directed-mint evidence hunts
stay TOC-based (skills already phrase it that way).
