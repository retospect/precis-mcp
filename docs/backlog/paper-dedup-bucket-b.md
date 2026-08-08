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
