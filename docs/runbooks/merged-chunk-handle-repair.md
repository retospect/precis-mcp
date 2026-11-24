# Repair `pc<id>` handles orphaned by pre-map merges (gr690836)

**Symptom.** `get(id='pc<N>')` answers NotFound for a chunk that was
cited, and the paper it belonged to shows 0 blocks or sits under a
merge tombstone. Known live instances (2026-11-18): `pc106519`,
`pc106517` (pa1181, Yao 1999, doi 10.1038/46241, cited by dr173020) and
`pc5667713..pc5667732` (pa536401 merged into pa207748, Krishnan 1997).

**Cause.** Merges that ran before the `chunk_supersede` map existed
(migration 0356) re-minted or tombstoned the loser's chunk ids with no
redirect written. New merges map at merge time; these two need a manual
backfill.

**Backup dependency.** The loser's chunk rows were DELETEd, so the
`old_chunk_id → (ord, text)` mapping survives only in a nightly dump.
Restore one into a throwaway container first
([`backup-restore-drill`](./backup-restore-drill.md), prod creds via
[`prod-db-access`](./prod-db-access.md)); pick a dump predating the
merge's `duplicate_merged` ref_event.

## Repair steps (release owner, prod)

1. Identify the loser and its survivor per instance:
   `SELECT ref_id, meta->>'superseded_by' FROM refs WHERE ref_id IN (<ids>)`
   (pa1181's survivor is the ref its `meta.superseded_by` names;
   pa536401 → pa207748).
2. From the restored dump, pull the loser's deleted body rows:
   `SELECT chunk_id, ord, text FROM chunks WHERE ref_id = <loser> AND ord >= 0`.
3. Write the map — same `(ord, md5(text))` match rule as
   `precis.ingest.body_ops.map_unmoved_body_chunks`:

   ```sql
   INSERT INTO chunk_supersede (old_chunk_id, new_chunk_id, src_ref_id, dst_ref_id)
   SELECT b.old_chunk_id, n.chunk_id, <loser>, <survivor>
   FROM recovered_chunks b
   JOIN chunks n ON n.ref_id = <survivor> AND n.ord = b.ord
     AND n.retired_at IS NULL AND md5(n.text) = md5(b.text)
   ON CONFLICT (old_chunk_id) DO NOTHING;
   ```

   Rows with no same-position same-text survivor chunk get no map row —
   they stay dead and `view='hygiene'` will name the pins dead. That is
   the honest report; do not map by position alone.
4. Verify: `get(id='pc106519')` and `get(id='pc106517')` resolve with a
   redirect marker; `get(kind='draft', id='dr173020', view='hygiene')`
   reports the pins as *merged* (or drops them once re-pinned). Then
   re-verify the four cited sentences in dr173020 — kink angles 36°/41°,
   ~500 devices, rectifying I-V — against the passages the handles now
   land on, and the pa207748 pins `pc5667713..pc5667732`.
