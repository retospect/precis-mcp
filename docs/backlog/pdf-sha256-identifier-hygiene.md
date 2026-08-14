---
status: draft
title: pdf_sha256 identifier hygiene — enforce one row per ref, backfill missing
model: sonnet
---

# pdf_sha256 identifier hygiene

Nanopub minting pins provenance to the sha256 of the exact PDF quoted
(`claim-publication-nanopub-ots.md` mint gates), which makes
`ref_identifiers` hygiene load-bearing. Two live defect classes found
while wargaming on prod (2026-08-14):

- **Duplicate rows** — ref 5937 (doi 10.1002/anie.202302693) carries TWO
  `pdf_sha256` rows (`fd750f05…` and `563dd4ec…`), a dup-ingest artifact.
  A mint must pin exactly one; ambiguity should be flagged upstream.
- **Missing row** — ref 42109 (doi 10.1073/pnas.2211786119) has ingested
  chunks but NO `pdf_sha256` identifier at all, so provenance can't pin
  the copy and the PDF deep link falls back to handle+chunk.

Work:

1. Sweep prod for both classes (refs with >1 `pdf_sha256`; refs with
   chunks but 0) — sizes the problem.
2. Ingest-side guard: a second differing sha for the same ref is a
   conflict to surface (re-ingest of a different copy), not a silent
   second row; chunk-producing ingest without a recorded sha is a bug.
3. Backfill where the PDF is still on disk (hash it); where it isn't,
   the ref stays unmintable — that's the mint gate working.
