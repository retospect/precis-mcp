---
status: idea
title: fetch-missing-DOIs button — phase 2 of the draft DOI-completeness check
---

# Fetch missing DOIs (phase 2)

Phase 1 shipped: DOI presence (pure read) + validity (network check,
`refs.doi_status` / `doi_validated_at`, migration 0132) ride the
`cited_paper_refs` walk beside retraction — report partitions, hygiene/
citations lines, and the watch button's shared network pass all live in
`src/precis/export/retraction.py` + `ingest/provenance.py`.

Remainder — the *fix* action, mirroring the retraction button:

- A "fetch missing DOIs" action that resolves the fetchable bucket
  (cites with an arxiv/pubmed/s2 id or title but no `doi`) by
  title+author via `ingest/paper_meta_enrich` + `crossref` /
  `openalex_meta`, TTL-gated with the same `select_for_check` per-press
  budget as `check_refs_retraction`, writing the resolved `doi` back
  onto the ref. Report still covers every cite; the network walk
  narrows to the neediest.
- Acceptance: pressing it resolves an arxiv-only cite to a DOI and the
  report re-reads clean; a no-identifier cite stays flagged.

Open questions (decide at implementation):
- Which id-kinds count as fetchable — gate on what `paper_meta_enrich`
  can actually resolve (arxiv + pubmed clearly; s2 / title-only?).
- Preprint / dataset / thesis entry types: flag with an entry-type note
  rather than silently exempt — the author decides.

Still out of scope: export-gating on missing/unvalidated DOI; deep
metadata correctness (resolve-vs-mismatch is a later follow-on); minting
DOIs.
