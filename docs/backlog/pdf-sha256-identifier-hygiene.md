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

- **Duplicate rows — no longer a defect class.** Two rows per ref is
  the metadata write-back's normal shape: `_maybe_patch_pdf` keeps the
  post-patch canonical sha in `refs.pdf_sha256` plus an as-downloaded
  alias identifier row so the dedup probe hits either byte sequence.
  `pdf_sha_rows` pins via `refs.pdf_sha256` when set (shipped
  ba75b7bd), and a 2026-08-17 prod sweep found **0** refs with >1
  identifier row and a NULL `refs.pdf_sha256` — nothing ambiguous
  remains. Only the ingest-side guard (work item 2) survives from this
  class.
- **Missing row** — ref 42109 (doi 10.1073/pnas.2211786119) has ingested
  chunks but NO `pdf_sha256` identifier at all, so provenance can't pin
  the copy and the PDF deep link falls back to handle+chunk. Sizing
  caveat (2026-08-17): 22,106 chunked refs have no sha anywhere, but
  most are legitimate PDF-less markup-first/OA-metadata ingests
  (`pdfless-not-a-junk-proxy`) — the defect subset is refs whose PDF
  **is** on disk (or whose chunks came through the Marker path) with no
  recorded sha; the sweep must join against corpus-dir presence, not
  just count NULLs.

Work:

1. Ingest-side guard: a second differing sha for the same ref outside
   the patch-alias pair is a conflict to surface (re-ingest of a
   different copy), not a silent second row; chunk-producing Marker
   ingest without a recorded sha is a bug.
2. Sweep the missing-row class properly (join corpus-dir / storage_path
   presence) and backfill where the PDF is still on disk (hash it);
   where it isn't, the ref stays unmintable — that's the mint gate
   working.
