-- 0364_chunk_supersede.sql
-- Chunk-level supersede map (gr690836): a body move INSERTs copies under
-- new chunk_ids and DELETEs the donor rows, so every ``pc<old>`` handle
-- pointed at a moved chunk dangled — ``resolve_handle`` only follows
-- ``meta.superseded_by`` for record handles. A merge records
-- ``old_chunk_id -> new_chunk_id`` here; resolve walks it (chains follow
-- transitively) and answers the new chunk with a redirect marker.
--
-- No FKs on purpose: ``old_chunk_id`` names a row ``move_body`` has
-- already DELETEd (a foreign key could never be inserted), and
-- ``new_chunk_id`` may itself be deleted by a later move — the next map
-- row carries the chain onward. ``src_ref_id``/``dst_ref_id`` keep the
-- provenance the merge ack and a later prod repair audit need.

CREATE TABLE IF NOT EXISTS chunk_supersede (
    old_chunk_id bigint PRIMARY KEY,
    new_chunk_id bigint NOT NULL,
    src_ref_id bigint NOT NULL,
    dst_ref_id bigint NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- Reverse direction for repair/audit reads ("which old handles land on
-- this chunk"); the forward walk uses the PK.
CREATE INDEX IF NOT EXISTS chunk_supersede_new_idx
    ON chunk_supersede (new_chunk_id);
