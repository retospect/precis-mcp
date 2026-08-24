-- 0136_fk_covering_indexes.sql
--
-- Covering indexes for the FK constraints `tests/test_schema_design.py`
-- flagged on its first run (2026-08-24). Postgres indexes the referenced
-- (parent) side of a FK automatically but never the referencing side, so
-- a parent DELETE/UPDATE seq-scans every child table that points at it.
--
-- Triage was by prod stats (parent delete traffic × child size), not
-- blanket coverage:
--
--   * INDEXED here — children of parents with a live delete path:
--     `refs` (paper deletes — including `nanopub_publish.claim_ref_id`,
--     whose only existing index is partial and thus unusable for the
--     cascade), `struct_atoms`/`struct_bonds` (structure
--     editing), pcb geometry parents, `nanopub_ots_batches` /
--     `nanopub_artifacts`. Every one of these child tables is empty or
--     tiny today (largest: nanopub_publish, 288 kB), so plain
--     transactional CREATE INDEX is instant — this ships the index
--     BEFORE the table is big, which is the whole point.
--
--   * NOT indexed (grandfathered in test_schema_design.py with this
--     rationale) — the provenance family: chunks/chunk_tags/ref_tags/
--     refs/links `set_by` → actors, chunk_summaries.summarizer,
--     chunk_embeddings.embedder. Parents are tiny append-only vocab
--     tables with zero deletes ever (pg_stat n_tup_del = 0); the
--     children total ~6.5 GB, so indexing them buys nothing and taxes
--     the ingest hot path's writes forever.
--
-- The composite pcb_netconns indexes list the FK's own column order;
-- the guard test only needs the FK columns as a leading prefix.
--
-- Second pass (review finding): partial indexes with a predicate on a
-- *different* column (`retired_at IS NULL`, `retired_version IS NULL`,
-- state filters) are unusable for the unqualified cascade lookup — the
-- planner can't prove the predicate, so retired rows still force a seq
-- scan on parent delete. The versioned-row families (cad/pcb/struct)
-- therefore get full FK indexes below, named `*_fk_idx` to distinguish
-- them from the live-row partials they coexist with. (Partials whose
-- predicate is `<fk-col> IS NOT NULL` are fine and got nothing.)
--
-- Forward-only (ADR 0005). Idempotent (IF NOT EXISTS).

BEGIN;

-- refs children (parent sees paper deletes)
CREATE INDEX IF NOT EXISTS material_values_source_ref_idx
    ON material_values (source_ref_id);
CREATE INDEX IF NOT EXISTS component_spec_values_source_ref_idx
    ON component_spec_values (source_ref_id);

-- component vocab
CREATE INDEX IF NOT EXISTS component_specs_category_idx
    ON component_specs (category_id);

-- structure editing (atom/bond deletes fan out to these)
CREATE INDEX IF NOT EXISTS struct_bonds_i_idx
    ON struct_bonds (i);
CREATE INDEX IF NOT EXISTS struct_bonds_j_idx
    ON struct_bonds (j);
CREATE INDEX IF NOT EXISTS struct_bond_atoms_atom_idx
    ON struct_bond_atoms (atom_id);
CREATE INDEX IF NOT EXISTS struct_measures_anchor_atom_idx
    ON struct_measures (anchor_atom_id);
CREATE INDEX IF NOT EXISTS struct_measures_anchor_bond_idx
    ON struct_measures (anchor_bond_id);

-- pcb geometry (component/pin deletes fan out to netconns)
CREATE INDEX IF NOT EXISTS pcb_netconns_instance_component_idx
    ON pcb_netconns (instance_id, component_id);
CREATE INDEX IF NOT EXISTS pcb_netconns_pin_component_idx
    ON pcb_netconns (pin_id, component_id);

-- nanopub publish back-pointers (tiny now, grows with the campaign)
CREATE INDEX IF NOT EXISTS nanopub_publish_artifact_idx
    ON nanopub_publish (artifact_id);
CREATE INDEX IF NOT EXISTS nanopub_publish_batch_idx
    ON nanopub_publish (batch_id);
-- claim_ref_id cascades from refs deletes; the existing
-- nanopub_publish_one_live_per_hub index is PARTIAL (predicate on state),
-- so the planner can't use it for the unqualified cascade lookup once
-- rows reach a terminal state — a full index is needed.
CREATE INDEX IF NOT EXISTS nanopub_publish_claim_ref_idx
    ON nanopub_publish (claim_ref_id);

-- versioned-row families: the existing (ref_id, ...) partials exclude
-- retired rows, so parent (refs / pcb_components) deletes need these
CREATE INDEX IF NOT EXISTS cad_nodes_ref_id_fk_idx
    ON cad_nodes (ref_id);
CREATE INDEX IF NOT EXISTS pcb_components_ref_id_fk_idx
    ON pcb_components (ref_id);
CREATE INDEX IF NOT EXISTS pcb_features_ref_id_fk_idx
    ON pcb_features (ref_id);
CREATE INDEX IF NOT EXISTS pcb_instances_ref_id_fk_idx
    ON pcb_instances (ref_id);
CREATE INDEX IF NOT EXISTS pcb_measures_ref_id_fk_idx
    ON pcb_measures (ref_id);
CREATE INDEX IF NOT EXISTS pcb_nets_ref_id_fk_idx
    ON pcb_nets (ref_id);
CREATE INDEX IF NOT EXISTS pcb_pins_component_id_fk_idx
    ON pcb_pins (component_id);
CREATE INDEX IF NOT EXISTS struct_atoms_ref_id_fk_idx
    ON struct_atoms (ref_id);
CREATE INDEX IF NOT EXISTS struct_bonds_ref_id_fk_idx
    ON struct_bonds (ref_id);
CREATE INDEX IF NOT EXISTS struct_measures_ref_id_fk_idx
    ON struct_measures (ref_id);

COMMIT;

-- End of 0136_fk_covering_indexes.sql
