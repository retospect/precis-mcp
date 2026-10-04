-- 0185_reviews_and_revisions.sql
--
-- One review ledger and one revision log for refs and links
-- (docs/backlog/local-mesh-upkeep.md §2 and §2b; Reto knowledge-mesh-10
-- and knowledge-mesh-11, 2026-10-03).
--
--   * `reviews` — who reviewed which target (chunk | ref | link), as a
--     human or as a model + version, at which content sha, with which
--     verdict (proposed | approved | rejected). A review is current while
--     its `content_sha` equals `precis_target_sha(kind, id)` now; an edit
--     makes it stale without any write path invalidating it. Plain-text
--     `actor`, not an FK: `actors` is the closed `set_by` vocabulary
--     (7 slugs, mirrored by `ActorSlug`), reviewers are open-ended
--     (personas, workers, humans).
--
--   * `revisions` — one row per transaction that changes a covered part
--     of a ref or link: the prior row (`prev_state`), its sha, the sha
--     after, and who / which model / why. Written by triggers, so no write
--     path has to remember. The reason, actor and model come from the
--     transaction-local settings `precis.reason`, `precis.actor`,
--     `precis.model` (`set_config(..., true)` — never a session SET; the
--     DSN is pgbouncer transaction pooling). A covered write with no
--     reason logs `(unrecorded)`.
--
--   * What counts as a change is what the sha covers: for refs, kind,
--     title, authors, year, parent_id, the kind's `kinds.covered_meta`
--     keys and the live body chunks' text; for links, both ends, the
--     relation and `precis_link_covered_meta()` keys. Meta is an
--     ALLOW-list: refs.meta is mostly machine state (106k job refs carry
--     lease keys, papers carry enrichment stamps; 125k ref updates in
--     prod stats), so a deny-list would flood the log. A kind keeps
--     history only when `kinds.covered_meta` is non-NULL ('{}' = columns
--     and body only).
--
--   * Body chunks are append-only (DELETE + INSERT). Statement triggers on
--     `chunks` fold a body replacement into the same transaction's ref
--     row, keeping the replaced text in `prev_state.chunks`. A deferred
--     seal computes `new_sha` at commit and drops a row whose content came
--     back to where it started.
--
--   * Every trigger catches its own errors and RAISEs a WARNING: a
--     revision that cannot be logged never fails the write.
--
-- The three older review shapes (chunk_review, links.meta.verified_by,
-- hub_refine's meta.last_refined_*) mirror into `reviews` by trigger
-- until their writers switch, and the first two are backfilled. hub_refine's
-- last_refined_* stamps are migrated lazily by the worker, which owns
-- claim_sha (blake2b, not computable here). links.meta.verified = true is
-- the ORCID authorship match, not a review, and is not backfilled.
--
--   * Lock order. Workers are live during migrate, so every statement that
--     locks refs / links / kinds / chunks / chunk_review (ALTER TABLE kinds,
--     CREATE and DROP TRIGGER, the refresh) runs LAST, after the new tables,
--     the functions and the two backfill scans. The backfill is a function,
--     precis_reviews_backfill(), idempotent and callable again after deploy:
--     stamps committed between its snapshot and the trigger creation are
--     caught by `SELECT precis_reviews_backfill()`. At runtime the trigger
--     refresh (precis_revision_triggers_refresh) takes ACCESS EXCLUSIVE on
--     refs and links only when the covered kind list really changes (rare);
--     the boot-time kinds upsert of every process leaves it untouched. A
--     failed refresh is recorded in revision_trigger_state.last_error. The
--     chunk statement triggers materialise old_rows text on a large DELETE
--     cascade from a non-covered ref (accepted).
--
-- Forward-only (ADR 0005). Idempotent: IF NOT EXISTS / OR REPLACE /
-- NOT EXISTS guards throughout.

BEGIN;

-- ── tables ─────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS reviews (
    review_id   bigserial PRIMARY KEY,
    target_kind text NOT NULL CHECK (target_kind IN ('chunk', 'ref', 'link')),
    target_id   bigint NOT NULL,
    actor       text NOT NULL CHECK (btrim(actor) <> ''),
    model       text,
    version     text NOT NULL DEFAULT '0',
    content_sha text NOT NULL,
    verdict     text NOT NULL
                CHECK (verdict IN ('proposed', 'approved', 'rejected')),
    note        text,
    at          timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE reviews IS
    'Review ledger (local-mesh-upkeep §2): one row per review of a chunk, '
    'ref or link. Current while content_sha = precis_target_sha(kind, id). '
    'model NULL = a human. No FK on target: reviews of a deleted link stay '
    'as audit.';

CREATE INDEX IF NOT EXISTS reviews_target_idx
    ON reviews (target_kind, target_id, at DESC);
CREATE INDEX IF NOT EXISTS reviews_actor_idx
    ON reviews (actor, verdict, at DESC);

CREATE TABLE IF NOT EXISTS revisions (
    revision_id bigserial PRIMARY KEY,
    target_kind text NOT NULL CHECK (target_kind IN ('ref', 'link')),
    target_id   bigint NOT NULL,
    at          timestamptz NOT NULL DEFAULT now(),
    xact        bigint NOT NULL DEFAULT txid_current(),
    event       text NOT NULL CHECK (event IN (
                    'edited', 'retired', 'restored', 'deleted', 'merged-into')),
    actor       text NOT NULL,
    model       text,
    reason      text NOT NULL,
    prev_sha    text,
    new_sha     text,
    prev_state  jsonb NOT NULL
);

COMMENT ON TABLE revisions IS
    'Revision log (local-mesh-upkeep §2b): one row per transaction that '
    'changed a covered part of a ref or link. prev_state = the full prior '
    'row (+ replaced body chunks under "chunks"). Written by triggers; '
    'reason from SET LOCAL precis.reason, else (unrecorded).';

CREATE UNIQUE INDEX IF NOT EXISTS revisions_target_xact_uq
    ON revisions (target_kind, target_id, xact);
CREATE INDEX IF NOT EXISTS revisions_target_at_idx
    ON revisions (target_kind, target_id, at);
CREATE INDEX IF NOT EXISTS revisions_unrecorded_idx
    ON revisions (at) WHERE reason = '(unrecorded)';

-- Which kind / key lists the revision triggers' WHEN clauses were last built
-- from (one row). precis_revision_triggers_refresh() compares against it and
-- does no DDL when nothing changed; a failed refresh records itself in
-- last_error so a stale WHEN list is never silent.
CREATE TABLE IF NOT EXISTS revision_trigger_state (
    singleton        boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    covered_kinds    text[],
    link_bookkeeping text[],
    refreshed_at     timestamptz NOT NULL DEFAULT now(),
    last_error       text,
    last_error_at    timestamptz
);

COMMENT ON TABLE revision_trigger_state IS
    'One row: the kinds.covered_meta kind list and the link bookkeeping-key '
    'list the revision triggers were last built from. last_error/last_error_at '
    'are set when a refresh after a kinds change failed (stale WHEN lists); '
    'cleared by the next successful refresh. Migration-managed, not data.';

-- ── the sha registry ───────────────────────────────────────────────────

CREATE OR REPLACE FUNCTION precis_pick_keys(m jsonb, keys text[])
    RETURNS jsonb LANGUAGE sql IMMUTABLE AS $$
    SELECT coalesce(jsonb_object_agg(k, m -> k), '{}'::jsonb)
      FROM unnest(keys) AS k
     WHERE m ? k
$$;

CREATE OR REPLACE FUNCTION precis_link_covered_meta()
    RETURNS text[] LANGUAGE sql IMMUTABLE AS $$
    SELECT ARRAY['support', 'support_reason', 'caveats', 'source_handle',
                 'quote', 'note', 'ruling']
$$;

-- plpgsql, not sql: the body is not resolved until first call, so this
-- can be created before `ALTER TABLE kinds ADD COLUMN covered_meta` (which
-- runs last in this file, after the table scans, to keep its lock short).
CREATE OR REPLACE FUNCTION precis_kind_covered_meta(p_kind text)
    RETURNS text[] LANGUAGE plpgsql STABLE AS $$
BEGIN
    RETURN (SELECT covered_meta FROM kinds WHERE slug = p_kind);
END
$$;

CREATE OR REPLACE FUNCTION precis_link_content(l links)
    RETURNS jsonb LANGUAGE sql IMMUTABLE AS $$
    SELECT jsonb_build_object(
        'src_ref_id', l.src_ref_id, 'src_chunk_id', l.src_chunk_id,
        'dst_ref_id', l.dst_ref_id, 'dst_chunk_id', l.dst_chunk_id,
        'relation', l.relation,
        'meta', precis_pick_keys(coalesce(l.meta, '{}'::jsonb),
                                 precis_link_covered_meta()))
$$;

CREATE OR REPLACE FUNCTION precis_ref_content(r refs)
    RETURNS jsonb LANGUAGE sql STABLE AS $$
    SELECT jsonb_build_object(
        'kind', r.kind, 'title', r.title, 'authors', r.authors,
        'year', r.year, 'parent_id', r.parent_id,
        'meta', precis_pick_keys(coalesce(r.meta, '{}'::jsonb),
                                 coalesce(precis_kind_covered_meta(r.kind),
                                          '{}'::text[])))
$$;

-- Hash of the live body chunks' text, in reading order.
CREATE OR REPLACE FUNCTION precis_body_sha(p_ref_id bigint)
    RETURNS text LANGUAGE sql STABLE AS $$
    SELECT md5(coalesce(string_agg(md5(c.text), ',' ORDER BY c.ord, c.chunk_id), ''))
      FROM chunks c
     WHERE c.ref_id = p_ref_id AND c.ord >= 0 AND c.retired_at IS NULL
$$;

CREATE OR REPLACE FUNCTION precis_ref_sha_of(r refs, p_body_sha text)
    RETURNS text LANGUAGE sql STABLE AS $$
    SELECT left(md5(precis_ref_content(r)::text || '|' || coalesce(p_body_sha, '')), 16)
$$;

CREATE OR REPLACE FUNCTION precis_link_sha_of(l links)
    RETURNS text LANGUAGE sql IMMUTABLE AS $$
    SELECT left(md5(precis_link_content(l)::text), 16)
$$;

-- The one sha reader for all three target kinds. NULL = target gone.
-- A body chunk is append-only, so its id stands for its content; a draft
-- chunk edits in place and carries content_sha.
CREATE OR REPLACE FUNCTION precis_target_sha(p_kind text, p_id bigint)
    RETURNS text LANGUAGE plpgsql STABLE AS $$
DECLARE
    s text;
BEGIN
    IF p_kind = 'chunk' THEN
        SELECT coalesce(c.content_sha, 'chunk:' || c.chunk_id) INTO s
          FROM chunks c WHERE c.chunk_id = p_id;
    ELSIF p_kind = 'ref' THEN
        SELECT precis_ref_sha_of(r, precis_body_sha(r.ref_id)) INTO s
          FROM refs r WHERE r.ref_id = p_id;
    ELSIF p_kind = 'link' THEN
        SELECT precis_link_sha_of(l) INTO s FROM links l WHERE l.link_id = p_id;
    END IF;
    RETURN s;
END
$$;

-- ── logging ────────────────────────────────────────────────────────────

CREATE OR REPLACE FUNCTION precis_revision_setting(p_name text)
    RETURNS text LANGUAGE sql STABLE AS $$
    SELECT nullif(current_setting('precis.' || p_name, true), '')
$$;

-- A ref or link created in this transaction and then changed is still a
-- creation: every trigger's WHEN skips rows with created_at = now().
--
-- Insert, or fold into this transaction's row for the target: the first
-- prior state and sha win; replaced body chunks are added once; a later
-- retire/restore/delete outranks an edit.
CREATE OR REPLACE FUNCTION precis_log_revision(
    p_kind text, p_id bigint, p_event text, p_prev_sha text, p_prev_state jsonb)
    RETURNS void LANGUAGE plpgsql AS $$
DECLARE
    v_event text := p_event;
BEGIN
    IF p_event IN ('edited', 'retired')
       AND precis_revision_setting('event') = 'merged-into' THEN
        v_event := 'merged-into';
    END IF;
    INSERT INTO revisions AS r
        (target_kind, target_id, event, actor, model, reason,
         prev_sha, prev_state)
    VALUES (p_kind, p_id, v_event,
            coalesce(precis_revision_setting('actor'), session_user::text),
            precis_revision_setting('model'),
            coalesce(precis_revision_setting('reason'), '(unrecorded)'),
            p_prev_sha, p_prev_state)
    ON CONFLICT (target_kind, target_id, xact) DO UPDATE SET
        event = CASE WHEN r.event = 'edited' THEN EXCLUDED.event ELSE r.event END,
        prev_state = CASE
            WHEN EXCLUDED.prev_state ? 'chunks' AND NOT r.prev_state ? 'chunks'
            THEN r.prev_state || jsonb_build_object('chunks', EXCLUDED.prev_state -> 'chunks')
            ELSE r.prev_state END;
END
$$;

-- Is this ref UPDATE a covered change? The trigger WHEN is only a cheap
-- prefilter (the kind list, see precis_revision_triggers_refresh); this is
-- the exact test, run in the trigger body.
CREATE OR REPLACE FUNCTION precis_ref_revision_due(o refs, n refs)
    RETURNS boolean LANGUAGE sql STABLE AS $$
    SELECT o.created_at < now()
       AND (precis_kind_covered_meta(o.kind) IS NOT NULL
            OR precis_kind_covered_meta(n.kind) IS NOT NULL)
       AND (precis_ref_content(o) IS DISTINCT FROM precis_ref_content(n)
            OR (o.retired_at IS NULL) <> (n.retired_at IS NULL))
$$;

CREATE OR REPLACE FUNCTION precis_refs_revision()
    RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    v_event text;
BEGIN
    -- The cheap due-test runs outside the exception block: plpgsql opens a
    -- savepoint on entry to a block with an EXCEPTION clause, and most
    -- covered-kind updates (bookkeeping meta) log nothing.
    IF TG_OP = 'UPDATE' AND NOT precis_ref_revision_due(OLD, NEW) THEN
        RETURN NULL;
    END IF;
    BEGIN
        IF TG_OP = 'DELETE' THEN
            PERFORM precis_log_revision(
                'ref', OLD.ref_id, 'deleted',
                precis_ref_sha_of(OLD, precis_body_sha(OLD.ref_id)), to_jsonb(OLD));
        ELSE
            v_event := CASE
                WHEN OLD.retired_at IS NULL AND NEW.retired_at IS NOT NULL THEN 'retired'
                WHEN OLD.retired_at IS NOT NULL AND NEW.retired_at IS NULL THEN 'restored'
                ELSE 'edited' END;
            PERFORM precis_log_revision(
                'ref', NEW.ref_id, v_event,
                precis_ref_sha_of(OLD, precis_body_sha(NEW.ref_id)), to_jsonb(OLD));
        END IF;
    EXCEPTION WHEN OTHERS THEN
        RAISE WARNING 'revisions: % of ref % not logged: %',
            TG_OP, OLD.ref_id, SQLERRM;
    END;
    RETURN NULL;
END
$$;

CREATE OR REPLACE FUNCTION precis_links_revision()
    RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'UPDATE' AND NOT (
           OLD.created_at < now()
           AND precis_link_content(OLD) IS DISTINCT FROM precis_link_content(NEW)) THEN
        RETURN NULL;
    END IF;
    BEGIN
        PERFORM precis_log_revision(
            'link', OLD.link_id,
            CASE WHEN TG_OP = 'DELETE' THEN 'deleted' ELSE 'edited' END,
            precis_link_sha_of(OLD), to_jsonb(OLD));
    EXCEPTION WHEN OTHERS THEN
        RAISE WARNING 'revisions: % of link % not logged: %',
            TG_OP, OLD.link_id, SQLERRM;
    END;
    RETURN NULL;
END
$$;

-- Body change on a ref that keeps history: fold into the ref's row. The
-- prior body is the live chunks now, plus the deleted rows (DELETE), minus
-- the inserted rows (INSERT). A ref created in this transaction is a
-- creation, not a revision. A cascaded delete (the ref is gone) is skipped;
-- the ref's own 'deleted' row covers it. The cheap affected-refs query runs
-- outside the exception block, so the ~all chunk statements that touch no
-- covered kind (paper ingest) open no savepoint.
CREATE OR REPLACE FUNCTION precis_chunks_body_revision()
    RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    v_ids bigint[];
    v_kinds text[];
    v_id bigint;
    v_ref refs;
    v_gone jsonb;
    v_prev_body text;
BEGIN
    SELECT array_agg(slug) INTO v_kinds FROM kinds WHERE covered_meta IS NOT NULL;
    IF v_kinds IS NULL THEN
        RETURN NULL;
    END IF;
    IF TG_OP = 'DELETE' THEN
        SELECT array_agg(DISTINCT r.ref_id) INTO v_ids
          FROM old_rows o JOIN refs r ON r.ref_id = o.ref_id
         WHERE o.ord >= 0 AND o.retired_at IS NULL AND r.created_at < now()
           AND r.kind = ANY (v_kinds);
    ELSE
        SELECT array_agg(DISTINCT r.ref_id) INTO v_ids
          FROM new_rows n JOIN refs r ON r.ref_id = n.ref_id
         WHERE n.ord >= 0 AND r.created_at < now()
           AND r.kind = ANY (v_kinds);
    END IF;
    IF v_ids IS NULL THEN
        RETURN NULL;
    END IF;
    BEGIN
        FOREACH v_id IN ARRAY v_ids LOOP
            SELECT * INTO v_ref FROM refs WHERE ref_id = v_id;
            IF TG_OP = 'DELETE' THEN
                SELECT jsonb_agg(jsonb_build_object(
                           'chunk_id', o.chunk_id, 'ord', o.ord,
                           'chunk_kind', o.chunk_kind, 'text', o.text)
                           ORDER BY o.ord, o.chunk_id)
                  INTO v_gone
                  FROM old_rows o
                 WHERE o.ref_id = v_id AND o.ord >= 0 AND o.retired_at IS NULL;
                SELECT md5(coalesce(string_agg(md5(t.text), ',' ORDER BY t.ord, t.chunk_id), ''))
                  INTO v_prev_body
                  FROM (SELECT c.chunk_id, c.ord, c.text FROM chunks c
                         WHERE c.ref_id = v_id AND c.ord >= 0 AND c.retired_at IS NULL
                        UNION ALL
                        SELECT o.chunk_id, o.ord, o.text FROM old_rows o
                         WHERE o.ref_id = v_id AND o.ord >= 0 AND o.retired_at IS NULL) t;
                PERFORM precis_log_revision(
                    'ref', v_id, 'edited', precis_ref_sha_of(v_ref, v_prev_body),
                    to_jsonb(v_ref) || jsonb_build_object('chunks', v_gone));
            ELSE
                SELECT md5(coalesce(string_agg(md5(c.text), ',' ORDER BY c.ord, c.chunk_id), ''))
                  INTO v_prev_body
                  FROM chunks c
                 WHERE c.ref_id = v_id AND c.ord >= 0 AND c.retired_at IS NULL
                   AND NOT EXISTS (SELECT 1 FROM new_rows n WHERE n.chunk_id = c.chunk_id);
                PERFORM precis_log_revision(
                    'ref', v_id, 'edited', precis_ref_sha_of(v_ref, v_prev_body),
                    to_jsonb(v_ref));
            END IF;
        END LOOP;
    EXCEPTION WHEN OTHERS THEN
        RAISE WARNING 'revisions: chunk % not logged: %', TG_OP, SQLERRM;
    END;
    RETURN NULL;
END
$$;

-- At commit: stamp new_sha, or drop a row whose content came back to
-- where it started (an edit and its undo in one transaction).
CREATE OR REPLACE FUNCTION precis_revisions_seal()
    RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    v_event text;
    v_prev text;
    v_now text;
BEGIN
    BEGIN
        SELECT event, prev_sha INTO v_event, v_prev
          FROM revisions WHERE revision_id = NEW.revision_id;
        IF NOT FOUND THEN
            RETURN NULL;
        END IF;
        v_now := precis_target_sha(NEW.target_kind, NEW.target_id);
        IF v_event = 'edited' AND v_now IS NOT DISTINCT FROM v_prev THEN
            DELETE FROM revisions WHERE revision_id = NEW.revision_id;
        ELSE
            UPDATE revisions SET new_sha = v_now WHERE revision_id = NEW.revision_id;
        END IF;
    EXCEPTION WHEN OTHERS THEN
        RAISE WARNING 'revisions: seal of % not done: %', NEW.revision_id, SQLERRM;
    END;
    RETURN NULL;
END
$$;

CREATE OR REPLACE FUNCTION precis_try_timestamptz(p text)
    RETURNS timestamptz LANGUAGE plpgsql IMMUTABLE AS $$
BEGIN
    RETURN p::timestamptz;
EXCEPTION WHEN OTHERS THEN
    RETURN NULL;
END
$$;

-- ── legacy stamps mirror into the ledger ───────────────────────────────
--
-- Until their writers and readers switch to `reviews`, the three older
-- review shapes keep being written. These triggers copy each new stamp
-- into the ledger, so it is complete from this migration on with no
-- Python write path changed. Drop them when the writers switch.

-- 'hub-refine' | '<model>/<actor>' | 'agent:<actor>' | a relayed Reto ruling.
CREATE OR REPLACE FUNCTION precis_reviewer_actor(p_stamp text)
    RETURNS text LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE
        WHEN p_stamp ILIKE '%reto ruling%' THEN 'reto'
        WHEN p_stamp ~ '^[^/ ]+/[^/ ]+$' THEN split_part(p_stamp, '/', 2)
        WHEN p_stamp LIKE 'agent:%' THEN substr(p_stamp, 7)
        ELSE p_stamp END
$$;

CREATE OR REPLACE FUNCTION precis_reviewer_model(p_stamp text)
    RETURNS text LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE WHEN p_stamp ~ '^[^/ ]+/[^/ ]+$' THEN split_part(p_stamp, '/', 1) END
$$;

-- chunk_review keeps one row per (chunk, checker); its verdict is free
-- text led by 'approved' or 'needs-rework'. A retraction (DELETE) is a
-- 'rejected' row: the checker withdrew the approval.
CREATE OR REPLACE FUNCTION precis_chunk_review_mirror()
    RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    BEGIN
        IF TG_OP = 'DELETE' THEN
            INSERT INTO reviews (target_kind, target_id, actor, content_sha, verdict, note)
            VALUES ('chunk', OLD.chunk_id, OLD.checker,
                    coalesce(OLD.approved_sha, '(none)'), 'rejected', '(retracted)');
        ELSE
            INSERT INTO reviews (target_kind, target_id, actor, content_sha, verdict, note, at)
            VALUES ('chunk', NEW.chunk_id, NEW.checker,
                    coalesce(NEW.approved_sha, '(none)'),
                    CASE WHEN NEW.verdict ILIKE 'approved%' THEN 'approved' ELSE 'rejected' END,
                    NEW.verdict, NEW.at);
        END IF;
    EXCEPTION WHEN OTHERS THEN
        RAISE WARNING 'reviews: chunk_review mirror not written: %', SQLERRM;
    END;
    RETURN NULL;
END
$$;

-- hub_refine stamps meta.last_refined_* on the hub it refined; the review
-- is of the hub as it stands after this write.
CREATE OR REPLACE FUNCTION precis_hub_refine_mirror()
    RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    BEGIN
        INSERT INTO reviews (target_kind, target_id, actor, version, content_sha, verdict, at)
        VALUES ('ref', NEW.ref_id, 'hub-refine',
                coalesce(NEW.meta ->> 'last_refined_version', '0'),
                precis_ref_sha_of(NEW, precis_body_sha(NEW.ref_id)), 'approved',
                coalesce(precis_try_timestamptz(NEW.meta ->> 'last_refined_at'), now()));
    EXCEPTION WHEN OTHERS THEN
        RAISE WARNING 'reviews: hub_refine mirror for ref % not written: %',
            NEW.ref_id, SQLERRM;
    END;
    RETURN NULL;
END
$$;

CREATE OR REPLACE FUNCTION precis_link_verified_mirror()
    RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    v_stamp text := btrim(NEW.meta ->> 'verified_by');
BEGIN
    IF coalesce(v_stamp, '') = '' THEN
        RETURN NULL;
    END IF;
    BEGIN
        INSERT INTO reviews
            (target_kind, target_id, actor, model, content_sha, verdict, note, at)
        VALUES ('link', NEW.link_id, precis_reviewer_actor(v_stamp),
                precis_reviewer_model(v_stamp), precis_link_sha_of(NEW), 'approved',
                'links.meta.verified_by=' || v_stamp
                    || coalesce(', verified_claim_sha=' || (NEW.meta ->> 'verified_claim_sha'), ''),
                coalesce(precis_try_timestamptz(NEW.meta ->> 'verified_at'), now()));
    EXCEPTION WHEN OTHERS THEN
        RAISE WARNING 'reviews: verified_by mirror for link % not written: %',
            NEW.link_id, SQLERRM;
    END;
    RETURN NULL;
END
$$;

-- ── backfill (a function: re-runnable after deploy) ───────────────────

-- Idempotent (NOT EXISTS guards). Run here, before any trigger exists, so no
-- lock on refs/links/kinds is held across these scans; stamps committed
-- between its snapshot and the trigger creation at the end of this file are
-- caught by running `SELECT precis_reviews_backfill()` once more after deploy.
CREATE OR REPLACE FUNCTION precis_reviews_backfill()
    RETURNS integer LANGUAGE plpgsql AS $$
DECLARE
    v_chunks integer;
    v_links integer;
BEGIN
    -- chunk_review rows (see precis_chunk_review_mirror for the verdict map).
    INSERT INTO reviews
        (target_kind, target_id, actor, model, version, content_sha, verdict, note, at)
    SELECT 'chunk', cr.chunk_id, cr.checker, NULL, '0',
           coalesce(cr.approved_sha, '(none)'),
           CASE WHEN cr.verdict ILIKE 'approved%' THEN 'approved' ELSE 'rejected' END,
           cr.verdict, cr.at
      FROM chunk_review cr
     WHERE NOT EXISTS (
            SELECT 1 FROM reviews v
             WHERE v.target_kind = 'chunk' AND v.target_id = cr.chunk_id
               AND v.actor = cr.checker AND v.at = cr.at);
    GET DIAGNOSTICS v_chunks = ROW_COUNT;

    -- links.meta.verified_by, at the link's sha now.
    INSERT INTO reviews
        (target_kind, target_id, actor, model, version, content_sha, verdict, note, at)
    SELECT 'link', l.link_id,
           precis_reviewer_actor(vb), precis_reviewer_model(vb),
           '0', precis_link_sha_of(l), 'approved',
           'backfill 0185 from links.meta.verified_by=' || vb
               || coalesce(', verified_claim_sha=' || (l.meta ->> 'verified_claim_sha'), ''),
           coalesce(precis_try_timestamptz(l.meta ->> 'verified_at'), l.created_at)
      FROM links l, LATERAL (SELECT btrim(l.meta ->> 'verified_by') AS vb) s
     WHERE coalesce(vb, '') <> ''
       AND NOT EXISTS (
            SELECT 1 FROM reviews v
             WHERE v.target_kind = 'link' AND v.target_id = l.link_id
               AND v.note LIKE 'backfill 0185 %');
    GET DIAGNOSTICS v_links = ROW_COUNT;
    RETURN v_chunks + v_links;
END
$$;

SELECT precis_reviews_backfill();

-- ── from here on: statements that take locks on kinds / refs / links ─────
--
-- Everything above only creates new tables, indexes on them, and functions,
-- then scans (backfill). The locks below (ACCESS EXCLUSIVE on kinds for the
-- ADD COLUMN, on refs / links / chunks / chunk_review for CREATE and DROP
-- TRIGGER) are taken last, so they are held for the length of the trigger
-- swap, not across a table scan. Workers are live during migrate.

ALTER TABLE kinds ADD COLUMN IF NOT EXISTS covered_meta text[];

COMMENT ON COLUMN kinds.covered_meta IS
    'Meta keys that are content for this kind (local-mesh-upkeep §2b). '
    'NULL = the kind keeps no revision history; ''{}'' = columns and body '
    'only. Read by precis_ref_content / the revisions triggers.';

UPDATE kinds SET covered_meta = ARRAY['scope', 'caveats']
 WHERE slug = 'finding' AND covered_meta IS NULL;
UPDATE kinds SET covered_meta = ARRAY['rule', 'warrant', 'hook']
 WHERE slug = 'memory' AND covered_meta IS NULL;
UPDATE kinds SET covered_meta = ARRAY['name', 'aliases', 'definition']
 WHERE slug = 'concept' AND covered_meta IS NULL;
UPDATE kinds SET covered_meta = ARRAY[
        'name', 'aliases', 'definition', 'status', 'value_type',
        'dimension_kind', 'canonical_unit', 'allowed_values',
        'higher_is_better', 'applies_to_ref', 'si_vector']
 WHERE slug = 'taxon' AND covered_meta IS NULL;
UPDATE kinds SET covered_meta = ARRAY[
        'claim', 'source_quote', 'char_offset', 'source_handle']
 WHERE slug = 'citation' AND covered_meta IS NULL;

-- Trigger WHEN clauses are prepared on every statement, even when they
-- short-circuit: a WHEN that calls a function (the SQL function is inlined,
-- ~45 us of parsing per statement) or lists meta keys (~3 us per term) taxed
-- every one-row bookkeeping UPDATE (125k refs updates in prod stats, ~106k
-- of them job refs that keep no history). So the hot-path WHENs are minimal
-- literal prefilters and the exact "is this a covered change" test runs in the
-- trigger body (precis_ref_revision_due / precis_link_content):
--   * refs: the covered-kind list. A second trigger on UPDATE OF kind catches
--     a ref moving INTO a covered kind (it is only evaluated when a statement
--     assigns `kind`).
--   * links: the meta minus the known bookkeeping keys must differ; a second
--     trigger on UPDATE OF the endpoint/relation columns catches those.
-- This function (re)creates them from `kinds.covered_meta`. It does nothing
-- (no DDL, no lock) when the lists equal revision_trigger_state, so the
-- boot-time `INSERT INTO kinds ... ON CONFLICT DO UPDATE` of every process is
-- free; it takes ACCESS EXCLUSIVE on refs and links only when the covered
-- lists really change, which is rare (a migration or an operator edit).
-- The function's own `SET lock_timeout` (scoped to the call) bounds the wait
-- for those locks: a busy refs/links makes the refresh fail after 3 s, and
-- the guarded kinds-trigger wrapper records that in
-- revision_trigger_state.last_error instead of stalling the kinds write.
-- No covered kind = no ref revision triggers. Returns true when it rebuilt.
CREATE OR REPLACE FUNCTION precis_revision_triggers_refresh()
    RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
    SET lock_timeout = '3s'
    SET search_path = public, pg_temp AS $$
DECLARE
    v_kinds text[];
    v_list text;
    -- Keys a worker stamps on links without changing their meaning. Only a
    -- prefilter: a key missing here costs one function call, never a wrong row.
    v_bookkeeping text[] := ARRAY['verified', 'verified_at', 'verified_by',
                                  'verified_claim_sha', 'candidate', 'elements'];
    v_state revision_trigger_state;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtext('precis_revision_triggers_refresh'));
    SELECT coalesce(array_agg(slug ORDER BY slug), '{}'::text[]) INTO v_kinds
      FROM kinds WHERE covered_meta IS NOT NULL;
    SELECT * INTO v_state FROM revision_trigger_state;
    IF FOUND
       AND v_state.covered_kinds IS NOT DISTINCT FROM v_kinds
       AND v_state.link_bookkeeping IS NOT DISTINCT FROM v_bookkeeping
       AND EXISTS (SELECT 1 FROM pg_trigger
                    WHERE tgname = 'links_revision_update'
                      AND tgrelid = 'public.links'::regclass)
       AND (cardinality(v_kinds) = 0
            OR EXISTS (SELECT 1 FROM pg_trigger
                        WHERE tgname = 'refs_revision_update'
                          AND tgrelid = 'public.refs'::regclass)) THEN
        -- triggers match the kinds: a stale last_error no longer applies
        IF v_state.last_error IS NOT NULL THEN
            UPDATE revision_trigger_state SET last_error = NULL, last_error_at = NULL;
        END IF;
        RETURN false;
    END IF;

    SELECT string_agg(format('%L', k), ', ' ORDER BY k) INTO v_list
      FROM unnest(v_kinds) AS k;

    DROP TRIGGER IF EXISTS refs_revision_update ON refs;
    DROP TRIGGER IF EXISTS refs_revision_kind ON refs;
    DROP TRIGGER IF EXISTS refs_revision_delete ON refs;
    IF v_list IS NOT NULL THEN
        EXECUTE format($t$
            CREATE TRIGGER refs_revision_update
                AFTER UPDATE ON refs FOR EACH ROW
                WHEN (OLD.kind IN (%s))
                EXECUTE FUNCTION precis_refs_revision()$t$, v_list);
        EXECUTE format($t$
            CREATE TRIGGER refs_revision_kind
                AFTER UPDATE OF kind ON refs FOR EACH ROW
                WHEN (NEW.kind IN (%s))
                EXECUTE FUNCTION precis_refs_revision()$t$, v_list);
        EXECUTE format($t$
            CREATE TRIGGER refs_revision_delete
                AFTER DELETE ON refs FOR EACH ROW
                WHEN (OLD.kind IN (%s) AND OLD.created_at < now())
                EXECUTE FUNCTION precis_refs_revision()$t$, v_list);
    END IF;

    DROP TRIGGER IF EXISTS links_revision_update ON links;
    DROP TRIGGER IF EXISTS links_revision_ends ON links;
    EXECUTE format($t$
        CREATE TRIGGER links_revision_update
            AFTER UPDATE ON links FOR EACH ROW
            WHEN ((OLD.meta - %1$L::text[]) IS DISTINCT FROM (NEW.meta - %1$L::text[]))
            EXECUTE FUNCTION precis_links_revision()$t$, v_bookkeeping);
    CREATE TRIGGER links_revision_ends
        AFTER UPDATE OF src_ref_id, src_chunk_id, dst_ref_id, dst_chunk_id, relation
        ON links FOR EACH ROW
        EXECUTE FUNCTION precis_links_revision();

    INSERT INTO revision_trigger_state
        (singleton, covered_kinds, link_bookkeeping, refreshed_at, last_error, last_error_at)
    VALUES (true, v_kinds, v_bookkeeping, now(), NULL, NULL)
    ON CONFLICT (singleton) DO UPDATE SET
        covered_kinds = EXCLUDED.covered_kinds,
        link_bookkeeping = EXCLUDED.link_bookkeeping,
        refreshed_at = EXCLUDED.refreshed_at,
        last_error = NULL, last_error_at = NULL;
    RETURN true;
END
$$;

-- The kinds triggers call it fault-tolerantly: a failed refresh never fails
-- the kinds write, but it leaves last_error / last_error_at in
-- revision_trigger_state (and a WARNING) so stale WHEN lists are visible.
CREATE OR REPLACE FUNCTION precis_kinds_refresh_guarded()
    RETURNS void LANGUAGE plpgsql SECURITY DEFINER
    SET search_path = public, pg_temp AS $$
BEGIN
    BEGIN
        PERFORM precis_revision_triggers_refresh();
    EXCEPTION WHEN OTHERS THEN
        RAISE WARNING 'revisions: trigger refresh after kinds change failed: %', SQLERRM;
        INSERT INTO revision_trigger_state (singleton, last_error, last_error_at)
        VALUES (true, SQLERRM, now())
        ON CONFLICT (singleton) DO UPDATE SET
            last_error = EXCLUDED.last_error, last_error_at = EXCLUDED.last_error_at;
    END;
END
$$;

-- Boot runs `INSERT INTO kinds ... ON CONFLICT (slug) DO UPDATE SET
-- is_numeric, title, description` once per registered kind (_kinds_ops.py).
-- None of these may fire the refresh on that path: UPDATE OF covered_meta
-- (the upsert's SET list does not name it), and INSERT / DELETE statement
-- triggers whose transition tables are checked for a covered row first
-- (transition tables allow one event per trigger, hence three).
CREATE OR REPLACE FUNCTION precis_kinds_covered_upd()
    RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM precis_kinds_refresh_guarded();
    RETURN NULL;
END
$$;

CREATE OR REPLACE FUNCTION precis_kinds_covered_ins()
    RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM new_rows WHERE covered_meta IS NOT NULL) THEN
        PERFORM precis_kinds_refresh_guarded();
    END IF;
    RETURN NULL;
END
$$;

CREATE OR REPLACE FUNCTION precis_kinds_covered_del()
    RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM old_rows WHERE covered_meta IS NOT NULL) THEN
        PERFORM precis_kinds_refresh_guarded();
    END IF;
    RETURN NULL;
END
$$;

DROP TRIGGER IF EXISTS kinds_covered_upd ON kinds;
CREATE TRIGGER kinds_covered_upd
    AFTER UPDATE OF covered_meta, slug ON kinds
    FOR EACH STATEMENT EXECUTE FUNCTION precis_kinds_covered_upd();

DROP TRIGGER IF EXISTS kinds_covered_ins ON kinds;
CREATE TRIGGER kinds_covered_ins
    AFTER INSERT ON kinds REFERENCING NEW TABLE AS new_rows
    FOR EACH STATEMENT EXECUTE FUNCTION precis_kinds_covered_ins();

DROP TRIGGER IF EXISTS kinds_covered_del ON kinds;
CREATE TRIGGER kinds_covered_del
    AFTER DELETE ON kinds REFERENCING OLD TABLE AS old_rows
    FOR EACH STATEMENT EXECUTE FUNCTION precis_kinds_covered_del();

SELECT precis_revision_triggers_refresh();

DROP TRIGGER IF EXISTS links_revision_delete ON links;
CREATE TRIGGER links_revision_delete
    AFTER DELETE ON links FOR EACH ROW
    WHEN (OLD.created_at < now())
    EXECUTE FUNCTION precis_links_revision();

-- Cost note: these statement triggers carry transition tables, so a large
-- DELETE cascade on chunks (e.g. dropping a non-covered ref with many chunks)
-- materialises all old_rows text before the covered-kind test can discard it.
DROP TRIGGER IF EXISTS chunks_body_revision_delete ON chunks;
CREATE TRIGGER chunks_body_revision_delete
    AFTER DELETE ON chunks REFERENCING OLD TABLE AS old_rows
    FOR EACH STATEMENT EXECUTE FUNCTION precis_chunks_body_revision();

DROP TRIGGER IF EXISTS chunks_body_revision_insert ON chunks;
CREATE TRIGGER chunks_body_revision_insert
    AFTER INSERT ON chunks REFERENCING NEW TABLE AS new_rows
    FOR EACH STATEMENT EXECUTE FUNCTION precis_chunks_body_revision();

DROP TRIGGER IF EXISTS revisions_seal ON revisions;
CREATE CONSTRAINT TRIGGER revisions_seal
    AFTER INSERT ON revisions DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION precis_revisions_seal();

DROP TRIGGER IF EXISTS chunk_review_mirror ON chunk_review;
CREATE TRIGGER chunk_review_mirror
    AFTER INSERT OR UPDATE OR DELETE ON chunk_review FOR EACH ROW
    EXECUTE FUNCTION precis_chunk_review_mirror();

DROP TRIGGER IF EXISTS refs_hub_refine_mirror ON refs;
CREATE TRIGGER refs_hub_refine_mirror
    AFTER UPDATE ON refs FOR EACH ROW
    WHEN (NEW.meta -> 'last_refined_at' IS NOT NULL
          AND NEW.meta -> 'last_refined_at' IS DISTINCT FROM OLD.meta -> 'last_refined_at')
    EXECUTE FUNCTION precis_hub_refine_mirror();

DROP TRIGGER IF EXISTS links_verified_mirror_insert ON links;
CREATE TRIGGER links_verified_mirror_insert
    AFTER INSERT ON links FOR EACH ROW
    WHEN (NEW.meta ? 'verified_by')
    EXECUTE FUNCTION precis_link_verified_mirror();

DROP TRIGGER IF EXISTS links_verified_mirror_update ON links;
CREATE TRIGGER links_verified_mirror_update
    AFTER UPDATE ON links FOR EACH ROW
    WHEN (NEW.meta ? 'verified_by'
          AND (NEW.meta -> 'verified_by' IS DISTINCT FROM OLD.meta -> 'verified_by'
               OR NEW.meta -> 'verified_at' IS DISTINCT FROM OLD.meta -> 'verified_at'))
    EXECUTE FUNCTION precis_link_verified_mirror();

COMMIT;
