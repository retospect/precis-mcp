-- 0261_ref_reads.sql
--
-- Per-ref read counters behind the eye header's age/read chip
-- (gr524009; spec consolidated in gr520321 comment 9 §3). A read is one
-- ``get``/``/eye`` call focused on the ref — any view, rung or chunk get
-- inside it — never a neighbour row, a 2hop count, a search hit or a
-- worker pass (the bump lives at the verb/route entry, never in a Store
-- fetch helper). ``chunks.accesses``/``last_seen`` can't supply this:
-- ``bump_salience`` (0137) moves them for search exposure too.
--
-- A narrow side table rather than ``refs`` columns: every read writes,
-- and ``refs`` is wide — a small row keeps updates HOT. Agent vs human
-- is split because "a human looked at this" is its own signal (human =
-- the web /eye under an authenticated user; agent = an MCP call).
-- ``prev_*_at`` carries the timestamp the current read displaced, so the
-- chip's "last" can render the previous read even when the renderer
-- reads the post-bump row.
--
-- The bump itself is the single row-locked upsert of gr520321 comment 5
-- (SET expressions read the pre-update row): +1 unless the same reader
-- read within 60s, ``prev_*_at := last_*_at``, ``last_*_at := now()``.
-- Wrapped in a SECURITY DEFINER function carrying
-- ``SET synchronous_commit = off`` — the read path never waits on a WAL
-- flush (a crash may lose a few bumps; acceptable) and the read-only
-- ``agent_ro`` role still counts (the 0137 pattern: REVOKE ALL, GRANT
-- EXECUTE TO PUBLIC — ``agent_ro``/``agent_rw`` are provisioned
-- out-of-tree, so the grant is to PUBLIC).
--
-- Forward-only (ADR 0005). Idempotent (IF NOT EXISTS / CREATE OR
-- REPLACE). No backfill: chunks.accesses is the wrong signal and a
-- tool_calls backfill is optional — counts start at deploy.

BEGIN;

CREATE TABLE IF NOT EXISTS ref_reads (
    ref_id bigint PRIMARY KEY REFERENCES refs ON DELETE CASCADE,
    n_agent bigint NOT NULL DEFAULT 0,
    n_human bigint NOT NULL DEFAULT 0,
    last_agent_at timestamptz,
    last_human_at timestamptz,
    prev_agent_at timestamptz,
    prev_human_at timestamptz,
    last_agent_reader text,
    last_human_reader text,
    first_read_at timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE ref_reads IS
    'Per-ref read counts for the eye header age/read chip (gr524009): '
    'one row per read focus ref, written once per get//eye call by '
    'bump_ref_read; never by search, ring renders or workers.';

CREATE OR REPLACE FUNCTION public.bump_ref_read(
    p_ref bigint, p_actor text, p_reader text
) RETURNS ref_reads
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path = public, pg_temp
    SET synchronous_commit = off
AS $$
DECLARE
    out_row ref_reads;
BEGIN
    IF p_actor = 'human' THEN
        INSERT INTO ref_reads AS r
            (ref_id, n_human, last_human_at, last_human_reader)
        VALUES (p_ref, 1, now(), p_reader)
        ON CONFLICT (ref_id) DO UPDATE SET
            n_human = r.n_human + (r.last_human_at IS NULL
                      OR r.last_human_at < now() - interval '60 seconds'
                      OR r.last_human_reader IS DISTINCT FROM
                         EXCLUDED.last_human_reader)::int,
            prev_human_at = r.last_human_at,
            last_human_at = now(),
            last_human_reader = EXCLUDED.last_human_reader
        RETURNING * INTO out_row;
    ELSE
        INSERT INTO ref_reads AS r
            (ref_id, n_agent, last_agent_at, last_agent_reader)
        VALUES (p_ref, 1, now(), p_reader)
        ON CONFLICT (ref_id) DO UPDATE SET
            n_agent = r.n_agent + (r.last_agent_at IS NULL
                      OR r.last_agent_at < now() - interval '60 seconds'
                      OR r.last_agent_reader IS DISTINCT FROM
                         EXCLUDED.last_agent_reader)::int,
            prev_agent_at = r.last_agent_at,
            last_agent_at = now(),
            last_agent_reader = EXCLUDED.last_agent_reader
        RETURNING * INTO out_row;
    END IF;
    RETURN out_row;
END
$$;

COMMENT ON FUNCTION public.bump_ref_read(bigint, text, text) IS
    'Count one read of a ref (gr524009): upsert ref_reads, +1 unless the '
    'same reader read within 60s, prev_*_at displaced. Returns the new '
    'row (render prev_*_at as "last"). SECURITY DEFINER + '
    'synchronous_commit=off so agent_ro counts and reads never wait on '
    'WAL; see migration 0137 for the grant pattern.';

REVOKE ALL ON FUNCTION public.bump_ref_read(bigint, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.bump_ref_read(bigint, text, text) TO PUBLIC;

COMMIT;

-- End of 0261_ref_reads.sql
