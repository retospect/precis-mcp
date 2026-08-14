# Dossier ledger nodes land as unpinned body chunks, stranding them

> Found 2026-08-14 on prod while trying to verify the dossier prose rewrite
> (`docs/backlog/verify-dossier-prose-rewrite-end-to-end.md`). Observed under
> **deployed** code, so this is live, not a stale-deploy artifact.

## Symptom

Dossier 202546 (owner quest 202469) carries **34 live body chunks
(`ord >= 0`, `retired_at IS NULL`) where the contract expects 1** plus pinned
extras. Thirty of them — chunks 2904088–2904117, `ord` 29–58 — were written in
a single burst at `2026-08-14 07:40:20–21`. Each is one ledger-node line,
23–148 chars:

```
29  NrfA/ccNiR mechanistic selectivity lessons for synthetic NO-to-NH3 catalysts
30  NrfA active-site structural dissection (distal heme pocket residues, …)
…
58  Selective electrochemical NO→NH2OH as primary product target (GDE / …)
```

All are `chunk_kind = 'paragraph'` with `meta = '{}'` — no pin marker, no
`role`. They are indistinguishable from narrative prose at the storage layer.

Spark's worker log names the consequence at the same timestamp:

```
2026-08-14 07:40:20,439 WARNING precis.quest.dossier dossier 202546 (owner
202469) has 13 unpinned body chunks; expected 1. Reading body[0] only — the
extras are stranded (never rewritten) and are NOT fed to the tick prompt.
```

So the ledger the quest is accumulating is **invisible to the next tick's
prompt**, and grows every time the ledger writer runs. The count in the warning
(13) is already stale against the 30 written minutes later in the same pass.

## Why this is the wrong shape

Two contracts collide:

- the dossier's one-body-chunk rule, which lets the rewrite DELETE+INSERT the
  narrative wholesale without stranding `chunk_embeddings` / `chunk_summaries`
  (see CLAUDE.md, "Don't mutate body chunks"); and
- the ledger, which wants durable per-node rows that *survive* a rewrite.

Pinning is what reconciles them, and the ledger writer isn't setting it. The
earlier "23 stranded chunks retired" cleanup on this dossier treated the
symptom: the same condition regrew within days because nothing changed on the
write path.

## What to check before fixing

The `dossier` module warns about "unpinned" chunks, so a pin concept exists —
find what it actually reads (it is not the `chunks.handle` column; every chunk
on this dossier has a distinct handle, retired and live alike, so `handle` is
not the marker). Whatever predicate that warning uses is the one the ledger
writer must satisfy. Fix the writer to emit pinned rows; do **not** widen the
one-body-chunk rule to tolerate 34.

Then decide the migration for existing rows: the 30 live ledger chunks on 202546
need pinning in place, or retiring plus a re-render — not a bare retire, which
loses the ledger.

## Verify

```sql
SELECT count(*) FROM chunks
 WHERE ref_id = 202546 AND ord >= 0 AND retired_at IS NULL;
```

Should be 1 + the pinned ledger nodes, and the `expected 1` warning should stop
appearing in spark's worker log for this dossier.

## Related

- `docs/backlog/verify-dossier-prose-rewrite-end-to-end.md` — the check this was
  found under; its criterion 3 assumed these chunks were already pinned.
- `docs/backlog/quest-compute-lane-runs-on-a-literature-only-quest.md` — same
  quest, unrelated defect.
