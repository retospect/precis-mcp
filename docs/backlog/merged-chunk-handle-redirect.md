# Chunk handle of a merged paper doesn't redirect

`resolve_handle` follows superseded_by for record handles only; a merged
paper's chunks are soft-deleted under different chunk_ids, so a pc<id>
dangles. A real fix needs a chunk-level supersede mapping written at merge
time — investigate before building. Design limitation, parked.
