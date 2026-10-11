---
status: ready
pillar: memory-graph
---

# Paper metadata resolution — run Bucket B + the titleless cohort on prod

Ops-gated resolve-metadata runs plus the standing-worker follow-up.

- `precis resolve-metadata` dry-run over the 94 needs-triage; inspect the
  auto/review/discard lanes, then --apply (network-bound, on-cluster;
  ~20 DOI-track + ~40 title-track expected auto).
- 187 titleless chunked papers: re-resolve by DOI (32) or S2 title search
  (≥0.85 gate) — dry-run → gold-check → --apply, then schedule into
  paper_reconcile (manual-only today).
- Build the standing worker for future id-less stubs after the CLI proves
  itself on prod. The 49 id-bearing stubs that title-match a held paper stay
  in the review lane — real merges need cross-id (S2) equivalence proof.
- Watcher NFS race: recognize the wrapped file-vanished error in
  `src/precis/cli/watch.py` and skip silently; verify the 7 split orphans
  self-heal post-deploy.

## Systemic: title auto-merge is unsafe and cannot be undone (gr264748)

Bundled in the 2026-10-11 gripe triage. Title-similarity auto-merge fuses
distinct papers and nothing can split them again.

- Tighten the title auto-merge gate (`_TITLE_AUTO_SIM` in
  `src/precis/ingest/dedup.py`) and require corroboration (year, first
  author, or a shared identifier) before an auto-merge; a bare title match
  goes to the review lane.
- Build a paper unmerge primitive. None exists: a wrong merge is permanent
  today. It must restore the absorbed ref's identifiers, chunks and links
  from the merge record.
- Data repair: fix pa2069 and pa42497 (wrongly merged), after the unmerge
  primitive exists.

Size: L. Who: agent-code (gate, corroboration, unmerge); the pa2069/pa42497
repair is ops after unmerge lands.
