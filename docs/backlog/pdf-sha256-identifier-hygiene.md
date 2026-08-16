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

- **Duplicate rows — mostly RESOLVED as by-design (2026-08-16).** Two
  rows per ref is the metadata write-back's normal shape:
  `_maybe_patch_pdf` keeps the post-patch canonical sha in
  `refs.pdf_sha256` plus an as-downloaded alias identifier row so the
  dedup probe hits either byte sequence. `pdf_sha_rows` now pins via
  `refs.pdf_sha256` when set (identifier rows are fallback), so alias
  pairs no longer trip the mint gate (bit on fi19981: refs 4185/5835/
  210108, and likely the wargamed ref 5937). Residual: refs with >1
  identifier row AND `refs.pdf_sha256` NULL are still genuinely
  ambiguous — sweep for those.
- **Missing row** — ref 42109 (doi 10.1073/pnas.2211786119) has ingested
  chunks but NO `pdf_sha256` identifier at all, so provenance can't pin
  the copy and the PDF deep link falls back to handle+chunk.

Work:

1. Sweep prod for the two live classes (refs with >1 `pdf_sha256` row
   and a NULL `refs.pdf_sha256`; refs with chunks but no sha anywhere)
   — sizes the problem.
2. Ingest-side guard: a second differing sha for the same ref outside
   the patch-alias pair is a conflict to surface (re-ingest of a
   different copy), not a silent second row; chunk-producing ingest
   without a recorded sha is a bug.
3. Backfill where the PDF is still on disk (hash it); where it isn't,
   the ref stays unmintable — that's the mint gate working.
