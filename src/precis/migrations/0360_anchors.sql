-- 0360_anchors.sql
--
-- Shared observations for pointers into text, code and structured refs (gr739606).
--
--   * anchors — immutable. Real columns, no JSON. Common: target_type,
--     quote (the text as observed), fingerprint, observed_at, note.
--     Position is three typed column sets and a CHECK that exactly one is
--     filled:
--       text — ref_id, chunk_id, char_start, char_end;
--              fingerprint = the chunk's content sha.
--       code — repo, symbol_path, line_start, line_end;
--              fingerprint = the commit sha the lines were read at.
--        part — ref_id, part_kind, selector; fingerprint = ref content sha.
--     "target" is the position set's ref_id / repo. chunk_id has no FK:
--     body chunks are append-only (DELETE + INSERT), so a re-ingest drops
--     the chunk the anchor was minted on and the resolve ladder finds the
--     quote again; an FK would block the re-ingest or erase the anchor.
--     A trigger refuses UPDATE: the row is what was observed (and what a
--     measure review sha may hash); movement lives in anchor_resolutions.
--
--   * anchor_resolutions — one row per anchor: where it is now and its
--     state (ok / moved / stale), with the ladder rung that decided it.
--     Rewritten by each relocate; the only mutable half.
--
-- The handle prefix mirrors target_type (text -> at, code -> ac, part -> ap, see
-- precis.utils.handle_registry) over the one anchor_id sequence; state is
-- never part of the handle. ``an`` is taken by the code-observation alias
-- ledger, so it is not used here.
--
-- Forward-only (ADR 0005). Idempotent.

BEGIN;

CREATE TABLE IF NOT EXISTS anchors (
    anchor_id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    target_type text NOT NULL CHECK (target_type IN ('text', 'code', 'part')),
    quote       text NOT NULL CHECK (quote <> ''),
    fingerprint text NOT NULL CHECK (fingerprint <> ''),
    observed_at timestamptz NOT NULL DEFAULT now(),
    note        text,
    ref_id      bigint REFERENCES refs (ref_id) ON DELETE CASCADE,
    chunk_id    bigint,
    char_start  integer,
    char_end    integer,
    repo        text,
    symbol_path text,
    line_start  integer,
    line_end    integer,
    part_kind   text,
    selector    text,
    CONSTRAINT anchors_one_position_set CHECK (
        (target_type = 'text'
            AND ref_id IS NOT NULL AND chunk_id IS NOT NULL
            AND char_start IS NOT NULL AND char_end IS NOT NULL
            AND char_start >= 0 AND char_end > char_start
            AND repo IS NULL AND symbol_path IS NULL
            AND line_start IS NULL AND line_end IS NULL
            AND part_kind IS NULL AND selector IS NULL)
        OR
        (target_type = 'code'
            AND repo IS NOT NULL AND symbol_path IS NOT NULL
            AND line_start IS NOT NULL AND line_end IS NOT NULL
            AND line_start >= 1 AND line_end >= line_start
            AND ref_id IS NULL AND chunk_id IS NULL
            AND char_start IS NULL AND char_end IS NULL
            AND part_kind IS NULL AND selector IS NULL)
        OR
        (target_type = 'part'
            AND ref_id IS NOT NULL
            AND part_kind IN ('se', 'structure', 'pathway', 'pcb')
            AND part_kind IS NOT NULL AND selector IS NOT NULL AND selector <> ''
            AND chunk_id IS NULL AND char_start IS NULL AND char_end IS NULL
            AND repo IS NULL AND symbol_path IS NULL
            AND line_start IS NULL AND line_end IS NULL)
    )
);

CREATE INDEX IF NOT EXISTS anchors_ref_id_idx
    ON anchors (ref_id) WHERE ref_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS anchors_repo_symbol_idx
    ON anchors (repo, symbol_path) WHERE repo IS NOT NULL;

CREATE OR REPLACE FUNCTION anchors_refuse_update() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'anchors rows are immutable; relocation is written to anchor_resolutions'
        USING ERRCODE = 'restrict_violation';
END;
$$;

DROP TRIGGER IF EXISTS anchors_immutable ON anchors;
CREATE TRIGGER anchors_immutable
    BEFORE UPDATE ON anchors
    FOR EACH ROW EXECUTE FUNCTION anchors_refuse_update();

CREATE TABLE IF NOT EXISTS anchor_resolutions (
    anchor_id   bigint PRIMARY KEY REFERENCES anchors (anchor_id) ON DELETE CASCADE,
    state       text NOT NULL CHECK (state IN ('ok', 'moved', 'stale')),
    method      text NOT NULL CHECK (method IN
                    ('fingerprint', 'verbatim', 'ambiguous', 'none')),
    chunk_id    bigint,
    char_start  integer,
    char_end    integer,
    line_start  integer,
    line_end    integer,
    symbol_path text,
    selector    text,
    resolved_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT anchor_resolutions_position CHECK (
        (state = 'stale'
            AND chunk_id IS NULL AND char_start IS NULL AND char_end IS NULL
            AND line_start IS NULL AND line_end IS NULL
            AND symbol_path IS NULL AND selector IS NULL)
        OR
        (state <> 'stale'
            AND chunk_id IS NOT NULL AND char_start IS NOT NULL
            AND char_end IS NOT NULL AND char_start >= 0 AND char_end > char_start
            AND line_start IS NULL AND line_end IS NULL
            AND symbol_path IS NULL AND selector IS NULL)
        OR
        (state <> 'stale'
            AND line_start IS NOT NULL AND line_end IS NOT NULL
            AND line_start >= 1 AND line_end >= line_start
            AND chunk_id IS NULL AND char_start IS NULL AND char_end IS NULL
            AND symbol_path IS NOT NULL AND selector IS NULL)
        OR
        (state <> 'stale'
            AND selector IS NOT NULL AND selector <> ''
            AND chunk_id IS NULL AND char_start IS NULL AND char_end IS NULL
            AND line_start IS NULL AND line_end IS NULL AND symbol_path IS NULL)
    )
);

CREATE OR REPLACE FUNCTION anchors_initial_resolution() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO anchor_resolutions
        (anchor_id, state, method, chunk_id, char_start, char_end, line_start,
         line_end, symbol_path, selector)
    VALUES (NEW.anchor_id, 'ok', 'fingerprint', NEW.chunk_id, NEW.char_start,
            NEW.char_end, NEW.line_start, NEW.line_end, NEW.symbol_path, NEW.selector);
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS anchors_create_resolution ON anchors;
CREATE TRIGGER anchors_create_resolution
    AFTER INSERT ON anchors
    FOR EACH ROW EXECUTE FUNCTION anchors_initial_resolution();

COMMENT ON TABLE anchors IS
    'Immutable relocatable pointer into text, code or a structured ref with the quote and fingerprint observed. Handle prefix mirrors target_type. Never updated; see anchor_resolutions.';
COMMENT ON COLUMN anchors.fingerprint IS
    'text: the chunk content sha at observation; code: the observed commit sha; part: the observed ref content sha.';
COMMENT ON TABLE anchor_resolutions IS
    'Current location and state (ok / moved / stale) of each anchor, with the resolve-ladder rung (method) that decided it. Embedding search only proposes candidates and never writes here.';

-- Side tables preserve the frozen and reviewed legacy measure fields and
-- the original code citation namespace.
CREATE TABLE IF NOT EXISTS measure_anchors (
    measure_id bigint REFERENCES measures (id) ON DELETE CASCADE,
    position integer CHECK (position >= 0),
    anchor_id bigint NOT NULL REFERENCES anchors (anchor_id) ON DELETE CASCADE,
    PRIMARY KEY (measure_id, position)
);

CREATE TABLE IF NOT EXISTS evidence_anchors (
    link_id bigint PRIMARY KEY REFERENCES links (link_id) ON DELETE CASCADE,
    anchor_id bigint NOT NULL REFERENCES anchors (anchor_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS code_anchor_aliases (
    ref_id bigint PRIMARY KEY REFERENCES refs (ref_id) ON DELETE CASCADE,
    anchor_id bigint NOT NULL REFERENCES anchors (anchor_id) ON DELETE CASCADE
);

DO $$
DECLARE
    observed record;
    minted bigint;
BEGIN
    FOR observed IN
        SELECT m.id, m.created_at, c.ref_id, c.chunk_id, c.text, c.content_sha,
               CASE WHEN m.span->>1 ~ '^[0-9]{1,9}$' THEN (m.span->>1)::integer END AS lo,
               CASE WHEN m.span->>2 ~ '^[0-9]{1,9}$' THEN (m.span->>2)::integer END AS hi
        FROM measures m
        JOIN links l ON l.link_id = m.primary_link_id
        JOIN chunks c ON c.chunk_id = l.src_chunk_id
        WHERE c.retired_at IS NULL AND m.anchor_scheme IN ('offsets', 'raw_offsets')
            AND CASE WHEN jsonb_typeof(m.span) = 'array'
                     THEN jsonb_array_length(m.span) = 3 ELSE false END
            AND m.span->>0 = c.chunk_id::text
            AND jsonb_typeof(m.span->1) = 'number' AND jsonb_typeof(m.span->2) = 'number'
            AND NOT EXISTS (
                SELECT 1 FROM measure_anchors a WHERE a.measure_id = m.id AND a.position = 0
            )
    LOOP
        IF observed.hi > observed.lo AND observed.lo < char_length(observed.text) THEN
            INSERT INTO anchors (target_type, quote, fingerprint, observed_at,
                                 ref_id, chunk_id, char_start, char_end)
            VALUES ('text', substr(observed.text, observed.lo + 1, observed.hi - observed.lo),
                    coalesce(nullif(observed.content_sha, ''),
                             encode(sha256(convert_to(observed.text, 'UTF8')), 'hex')),
                    observed.created_at, observed.ref_id, observed.chunk_id, observed.lo,
                    least(observed.hi, char_length(observed.text)))
            RETURNING anchor_id INTO minted;
            INSERT INTO measure_anchors (measure_id, position, anchor_id)
            VALUES (observed.id, 0, minted);
        END IF;
    END LOOP;
END;
$$;

DO $$
DECLARE
    observed record;
    minted bigint;
BEGIN
    FOR observed IN
        SELECT ref_id, created_at, meta->'code_anchor' AS payload
        FROM refs
        WHERE kind = 'alias' AND meta->'alias'->>'a' ~* '^an[0-9]+$'
            AND jsonb_typeof(meta->'code_anchor'->'snippet') = 'array'
            AND NOT EXISTS (SELECT 1 FROM code_anchor_aliases a
                            WHERE a.ref_id = refs.ref_id)
    LOOP
        IF observed.payload->>'start' !~ '^[1-9][0-9]{0,8}$'
            OR nullif(observed.payload->>'sha', '') IS NULL
            OR nullif(observed.payload->>'repo', '') IS NULL
            OR nullif(observed.payload->>'file', '') IS NULL
            OR nullif(observed.payload->>'qualname', '') IS NULL THEN
            CONTINUE;
        END IF;
        INSERT INTO anchors
            (target_type, quote, fingerprint, note, observed_at, repo,
             symbol_path, line_start, line_end)
        SELECT 'code', string_agg(line, E'\n' ORDER BY ord), observed.payload->>'sha',
               observed.payload->>'note', observed.created_at, observed.payload->>'repo',
               concat_ws('::', observed.payload->>'file', observed.payload->>'qualname'),
               (observed.payload->>'start')::integer,
               (observed.payload->>'start')::integer + count(*)::integer - 1
        FROM jsonb_array_elements_text(observed.payload->'snippet')
             WITH ORDINALITY AS snippet(line, ord)
        WHERE observed.payload->>'start' IS NOT NULL
        HAVING string_agg(line, E'\n' ORDER BY ord) <> ''
        RETURNING anchor_id INTO minted;
        IF minted IS NOT NULL THEN
            INSERT INTO code_anchor_aliases (ref_id, anchor_id) VALUES (observed.ref_id, minted);
        END IF;
    END LOOP;
END;
$$;

COMMIT;

-- End of 0360_anchors.sql
