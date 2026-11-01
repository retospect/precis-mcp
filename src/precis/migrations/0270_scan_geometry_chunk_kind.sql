-- 0270_scan_geometry_chunk_kind.sql
--
-- gr540872 (Reto 2026-10-31, storage option (a)): a surface_coverage_scan
-- job keeps every scan point's final relaxed geometry, and for a failed
-- point (unconverged, detached, placement clash) its relax trajectory
-- subsampled to <= 50 frames, as job chunks of their own kind. The engine
-- emits them with autocatpath.coverage.scan(keep_geometry=True)
-- (catpath 423c4fa); precis/store/scan_geometry.py stores them.
--
-- Shape of the cad `field` chunk (store/_cad_ops.py::put_field): the
-- chunk's `text` is a one-line, float-free summary ("<point id> n=<n>
-- final|frames failed=<bool>") and the extxyz itself sits in chunk_blobs
-- (0035), so the lexical tsvector over chunks.text never sees coordinates
-- and the GIN index gains no float lexemes. The ruling's three conditions:
--   1. out of embedding and search -- EmbedHandler / RakeLemmaHandler
--      skip_chunk_kinds, chunk_keywords / llm_summarize SKIP_KINDS, and
--      meta.no_index on every chunk; the body text is a summary line only.
--   2. read through job view='geometry' (list points / fetch one), never
--      inline: the default job card renders job_summary / job_event /
--      job_result only.
--   3. append-only: a re-run appends new chunks; nothing updates or
--      deletes them. They live and retire with their job ref (a retired
--      ref keeps its chunks; a hard ref delete cascades chunk -> blob).
--
-- is_card FALSE: telemetry/provenance, not a search card (mirrors run_log,
-- 0120). Forward-only (ADR 0005). Idempotent. Regenerate the baseline
-- snapshot at release time (ADR 0031): `scripts/bump` / `precis db
-- dump-schema`.

BEGIN;

INSERT INTO chunk_kinds (slug, is_card, description) VALUES
    ('scan_geometry', FALSE,
     'One coverage-scan point''s relaxed geometry (extxyz in chunk_blobs): '
     'the final structure, or a failed point''s subsampled relax frames. '
     'Read via get(kind=''job'', view=''geometry''); never embedded or '
     'searched (gr540872).')
ON CONFLICT (slug) DO NOTHING;

COMMIT;

-- End of 0270_scan_geometry_chunk_kind.sql
