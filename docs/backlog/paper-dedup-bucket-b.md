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

## Systemic: paper unmerge + bad-merge data repair (gr264748)

The corroboration gates shipped (title auto-merge needs >=5 title tokens,
matching known years and agreeing first author; the orphan-stub filename/handle
fold needs title overlap with the PDF). The title gate (d3d8569ef) stops new
bad merges.

RULED (Reto 2026-10-12, design-fold): repair by hand first; build unmerge
only if the damage is wider. Old merges recorded nothing about what they
moved, so a generic unmerge cannot reverse them anyway.

1. Data repair by hand: pa2069 (handle collision, ~25 rows in dc1519359 cite
   it for unrelated papers) and pa42497->pa42939 (wrongly merged). Restore
   identifiers, chunks and links from the source PDFs and the dc1519359
   rows.
2. DONE (read-only sweep, 2026-10-21, gr501446): pa4249x range has 5 merged
   pairs; 3 look wrong, 1 is the already-known pa42497->pa42939 pair above.
   2 further bad merges found — pa42495->pa42940 and pa42498->pa44470 — at
   or below the gate, so step 3 does not trigger. pa42498's merge came via
   `reconcile-doi-case`, not the content-duplicate-stub path the title gate
   (d3d8569ef) corroborates — flagged in gr501446 as a possible second gap,
   not yet swept.
3. Gate not tripped (2 further bad merges, not >2): no `unmerge_duplicate`
   build from this sweep. pa42495 and pa42498 need the same by-hand repair
   as step 1 (gr501446).

Size: S (steps 1–2, done except hand repair of the 2 new finds). Who: ops
for 1–2 (including the 2 new finds), agent-code only if a future sweep of
`reconcile-doi-case` merges turns up more than 2 further bad ones.
