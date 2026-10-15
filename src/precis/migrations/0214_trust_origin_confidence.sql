-- 0214_trust_origin_confidence.sql
--
-- Graph trust state, slice 1 (docs/backlog/graph-node-trust-state.md;
-- Reto 2026-10-05: refs and links only). Every ref and link carries a
-- non-NULL origin and confidence; legacy rows are honestly unknown.
--
--   * refs / links gain three constant-default columns (metadata-only ADD
--     COLUMN): trust_origin (human | extracted | computed | generated |
--     unknown), trust_confidence (hunch | considered | unknown) and
--     trust_write_version (0 = unstamped legacy or compatibility write, must
--     be unknown/unknown; 1 = this write passed the contract). The CHECKs go
--     in NOT VALID here and are validated by 0215 in its own transaction, so
--     the scan never runs under this file's ACCESS EXCLUSIVE locks.
--
--   * The stamp comes from the write context, never from the row: a
--     converted writer sends the transaction-local settings
--     precis.trust_origin, precis.trust_actor_type and (for an explicit act)
--     precis.trust_act, next to 0185's precis.reason/actor/model
--     (precis.store.revision_context). BEFORE triggers derive the columns;
--     a raw INSERT/UPDATE naming trust values is refused. GUCs are transport,
--     not authentication: the shared DB role can set them, so the ingress
--     rules in Python (no MCP-asserted human origin, operator-only legacy and
--     origin-correction acts) are the capability boundary in this slice.
--
--   * Writer contract. With a context: a new row is <origin>/hunch v1
--     ('create'); a semantic change re-stamps <origin>/hunch v1 ('revise'),
--     so considered never survives a content change by id alone. Without a
--     context (an unconverted writer, compatibility mode): a new row is
--     unknown/unknown v0 and a v0 row may change; a semantic change to a v1
--     row, or to its live body, is refused. Bookkeeping updates (meta keys
--     outside the 0185 content projection, retire/restore) keep the stamp.
--     Strict mode (precis_trust_activate_strict, run by the coordinator once
--     every writer is converted) refuses any context-less ref/link write and
--     any context-less semantic change.
--
--   * Explicit acts (precis.trust_act JSON): consider, demote,
--     correct-origin, legacy-import, carry. A targeted act names its target
--     and the expected content sha (precis_target_sha) and is refused when
--     stale; a create-time act binds to the first row of the named ref kind
--     or link relation inserted in the transaction. Considered needs a
--     consider act with a reason. The same op_id with the same payload is a
--     replay (no new act); a changed payload conflicts.
--
--   * Acts land in revisions.trust_acts (ordered JSONB array, one revisions
--     row per target per transaction as before; event 'trust-recorded' when
--     no content event exists). Appended by AFTER triggers, so a no-op
--     upsert or a rolled-back statement records nothing. The sealer keeps a
--     row with acts even when edit + undo returns the content sha to where
--     it started, and drops 'create' acts whose target was not really
--     created in this transaction (an INSERT ... ON CONFLICT DO UPDATE).
--
--   * Body: chunk statement/row triggers guard the owning ref (ord >= 0
--     rows; INSERT, DELETE and UPDATE OF ref_id/ord/text/retired_at, both
--     owners on a move). Derived chunks (ord < 0) and appended timeline
--     entries (comments, job events/summaries, logs; the explicit list in
--     precis_trust_subject_chunk) are not the ref's trust subject. No chunk
--     columns, no backfill.
--
-- Forward-only (ADR 0005). Replaces 0185's precis_log_revision and
-- precis_revisions_seal (sealed file untouched). The table locks are taken
-- last, revisions after refs / links / chunks, under a 10s lock_timeout:
-- apply in a quiet window (a timeout rolls the whole file back).

BEGIN;

-- ── mode ───────────────────────────────────────────────────────────────

-- No row until the strict switch writes one: a missing row reads as compat.
-- The compatibility window starts at this migration's _migrations.applied_at.
CREATE TABLE IF NOT EXISTS trust_write_state (
    singleton   boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    mode        text NOT NULL DEFAULT 'compat' CHECK (mode IN ('compat', 'strict')),
    strict_at   timestamptz
);

COMMENT ON TABLE trust_write_state IS
    'At most one row: graph-node-trust-state slice 1 write mode. compat (or '
    'no row) = context-less ref/link writes land unknown/unknown v0; strict = '
    'they are refused. Written only by precis_trust_activate_strict().';

-- ── vocabulary and context ─────────────────────────────────────────────

CREATE OR REPLACE FUNCTION precis_trust_strict()
    RETURNS boolean LANGUAGE sql STABLE AS $$
    SELECT coalesce((SELECT mode = 'strict' FROM trust_write_state), false)
$$;

CREATE OR REPLACE FUNCTION precis_trust_refuse(p_msg text, p_hint text)
    RETURNS void LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'trust: %', p_msg
        USING ERRCODE = 'check_violation', HINT = p_hint;
END
$$;

-- The explicit act this transaction carries, or NULL. Shape-checked here so
-- every trigger sees the same validated payload.
CREATE OR REPLACE FUNCTION precis_trust_ctx_act()
    RETURNS jsonb LANGUAGE plpgsql STABLE AS $$
DECLARE
    v_raw text := precis_revision_setting('trust_act');
    v_act jsonb;
BEGIN
    IF v_raw IS NULL THEN
        RETURN NULL;
    END IF;
    BEGIN
        v_act := v_raw::jsonb;
    EXCEPTION WHEN OTHERS THEN
        PERFORM precis_trust_refuse('precis.trust_act is not JSON',
            'send acts through precis.store.revision_context.TrustAct');
    END;
    IF jsonb_typeof(v_act) <> 'object'
       OR NOT (v_act ->> 'action') = ANY (ARRAY[
           'consider', 'demote', 'correct-origin', 'legacy-import', 'carry'])
       OR NOT (v_act ->> 'target_kind') = ANY (ARRAY['ref', 'link'])
       OR coalesce(btrim(v_act ->> 'reason'), '') = ''
       OR coalesce(btrim(v_act ->> 'op_id'), '') = '' THEN
        PERFORM precis_trust_refuse(
            format('malformed trust act %s', v_raw),
            'an act needs action, target_kind, op_id and a non-empty reason');
    END IF;
    RETURN v_act;
END
$$;

CREATE OR REPLACE FUNCTION precis_trust_ctx_origin()
    RETURNS text LANGUAGE plpgsql STABLE AS $$
DECLARE
    v text := precis_revision_setting('trust_origin');
BEGIN
    IF v IS NOT NULL AND NOT v = ANY (ARRAY['human', 'extracted', 'computed', 'generated']) THEN
        PERFORM precis_trust_refuse(
            format('invalid write-context origin %L', v),
            'origin is human, extracted, computed or generated; unknown only '
            'through a legacy-import or carry act');
    END IF;
    RETURN v;
END
$$;

-- Converted ref kinds: their verb path and every non-verb writer run in a
-- trust context (Handler.trust_origin in Python; tests pin the two lists
-- equal). A ref of any other kind ignores the write context and stays v0,
-- so a door that also touches, say, a paper does not lift that paper to v1
-- ahead of the paper writers (enrichers, ingest), which would then be
-- refused. Converting a kind is a new migration that replaces this list.
CREATE OR REPLACE FUNCTION precis_trust_kind_converted(p_kind text)
    RETURNS boolean LANGUAGE sql IMMUTABLE AS $$
    SELECT p_kind = ANY (ARRAY['memory', 'finding', 'concept', 'taxon', 'todo', 'gripe'])
$$;

-- A link is converted when either end is a ref of a converted kind; in
-- strict mode every ref and link is.
CREATE OR REPLACE FUNCTION precis_trust_link_converted(p_ref_ids bigint[])
    RETURNS boolean LANGUAGE sql STABLE AS $$
    SELECT precis_trust_strict()
        OR EXISTS (SELECT 1 FROM refs
                    WHERE ref_id = ANY (p_ref_ids)
                      AND precis_trust_kind_converted(kind))
$$;

-- Semantic change = what 0185's content sha covers (kind, title, authors,
-- year, parent_id, the kind's covered meta; ends, relation, covered link
-- meta). Retire/restore is lifecycle, not content.
CREATE OR REPLACE FUNCTION precis_trust_ref_semantic(o refs, n refs)
    RETURNS boolean LANGUAGE sql STABLE AS $$
    SELECT (o.kind, o.title, o.authors, o.year, o.parent_id)
               IS DISTINCT FROM (n.kind, n.title, n.authors, n.year, n.parent_id)
        OR (o.meta IS DISTINCT FROM n.meta
            AND precis_ref_content(o) IS DISTINCT FROM precis_ref_content(n))
$$;

-- The act this op_id already recorded on the target, or NULL. plpgsql so
-- the body (revisions.trust_acts, added at the end of this file) resolves
-- at first call, not at CREATE.
CREATE OR REPLACE FUNCTION precis_trust_recorded_act(
    p_kind text, p_id bigint, p_op_id text)
    RETURNS jsonb LANGUAGE plpgsql STABLE AS $$
BEGIN
    RETURN (SELECT a
              FROM revisions r, jsonb_array_elements(r.trust_acts) a
             WHERE r.target_kind = p_kind AND r.target_id = p_id
               AND a ->> 'op_id' = p_op_id
             ORDER BY r.revision_id
             LIMIT 1);
END
$$;

-- Does the context's act target this row? For an INSERT, the first row of
-- the act's create_match (ref kind / link relation) in this transaction
-- binds it (precis.trust_act_bound).
CREATE OR REPLACE FUNCTION precis_trust_act_targets(
    p_act jsonb, p_kind text, p_id bigint, p_insert boolean, p_match text)
    RETURNS boolean LANGUAGE plpgsql STABLE AS $$
BEGIN
    IF p_act IS NULL OR p_act ->> 'target_kind' <> p_kind THEN
        RETURN false;
    END IF;
    IF p_insert THEN
        RETURN p_act ->> 'target_id' IS NULL
           AND p_act ->> 'create_match' IS NOT DISTINCT FROM p_match;
    END IF;
    RETURN (p_act ->> 'target_id')::bigint = p_id;
END
$$;

-- ── the decision (BEFORE triggers) ─────────────────────────────────────
--
-- Returns {origin, confidence, version}. Raises on every refusal.

CREATE OR REPLACE FUNCTION precis_trust_decide(
    p_kind text, p_id bigint, p_insert boolean, p_match text,
    p_old_origin text, p_old_conf text, p_old_version smallint,
    p_new_origin text, p_new_conf text, p_new_version smallint,
    p_semantic boolean, p_old_sha text, p_converted boolean)
    RETURNS text[] LANGUAGE plpgsql AS $$
DECLARE
    v_origin text := CASE WHEN p_converted THEN precis_trust_ctx_origin() END;
    v_act jsonb := precis_trust_ctx_act();
    v_action text;
    v_prior jsonb;
    v_src record;
    v_target boolean;
    v_raw boolean;
BEGIN
    -- the body guard re-stamps through this door with its own act
    IF NOT p_insert AND precis_revision_setting('trust_internal') = 'on' THEN
        RETURN ARRAY[p_new_origin, p_new_conf, p_new_version::text];
    END IF;

    v_raw := CASE WHEN p_insert
        THEN (p_new_origin, p_new_conf, p_new_version) IS DISTINCT FROM
             ('unknown', 'unknown', 0::smallint)
        ELSE (p_new_origin, p_new_conf, p_new_version) IS DISTINCT FROM
             (p_old_origin, p_old_conf, p_old_version) END;
    IF v_raw THEN
        PERFORM precis_trust_refuse(
            format('%s %s: trust columns are set only by the write context, '
                   'not by the statement', p_kind, p_id),
            'wrap the write in revision_context(trust_origin=...) or record an '
            'explicit TrustAct; raw trust_* values are refused');
    END IF;

    v_target := precis_trust_act_targets(v_act, p_kind, p_id, p_insert, p_match)
        AND (NOT p_insert OR precis_revision_setting('trust_act_bound') IS NULL);

    IF v_target AND NOT p_converted THEN
        PERFORM precis_trust_refuse(
            format('%s %s is not trust-converted yet; act %s cannot apply',
                   p_kind, coalesce(p_id::text, p_match), v_act ->> 'action'),
            'acts apply to refs of the converted kinds and links touching them '
            '(precis_trust_kind_converted)');
    END IF;

    IF v_target THEN
        v_action := v_act ->> 'action';
        IF p_insert THEN
            PERFORM set_config('precis.trust_act_bound', p_id::text, true);
            IF v_action = 'consider' THEN
                IF v_origin IS NULL THEN
                    PERFORM precis_trust_refuse(
                        'considered on create needs a write-context origin',
                        'set revision_context(trust_origin=...) with the act');
                END IF;
                RETURN ARRAY[v_origin, 'considered', '1'];
            ELSIF v_action = 'legacy-import' THEN
                RETURN ARRAY[coalesce(v_act ->> 'origin', 'unknown'),
                             coalesce(v_act ->> 'confidence', 'unknown'), '1'];
            ELSIF v_action = 'carry' THEN
                IF p_kind = 'ref' THEN
                    SELECT trust_origin, trust_confidence, trust_write_version
                      INTO v_src FROM refs
                     WHERE ref_id = (v_act ->> 'carry_from')::bigint;
                ELSE
                    SELECT trust_origin, trust_confidence, trust_write_version
                      INTO v_src FROM links
                     WHERE link_id = (v_act ->> 'carry_from')::bigint;
                END IF;
                IF NOT FOUND THEN
                    PERFORM precis_trust_refuse(
                        format('carry names %s %s, which does not exist',
                               p_kind, v_act ->> 'carry_from'),
                        'carry_from must be an existing predecessor of the same kind');
                END IF;
                IF v_src.trust_write_version = 0 THEN
                    RETURN ARRAY['unknown', 'unknown', '1'];
                END IF;
                RETURN ARRAY[v_src.trust_origin, v_src.trust_confidence, '1'];
            END IF;
            PERFORM precis_trust_refuse(
                format('act %s cannot apply to a new %s', v_action, p_kind),
                'create-time acts are consider, legacy-import and carry');
        END IF;

        -- targeted act on an existing row: replay, staleness, then the act
        v_prior := precis_trust_recorded_act(p_kind, p_id, v_act ->> 'op_id');
        IF v_prior IS NOT NULL THEN
            IF (v_prior -> 'payload') IS DISTINCT FROM jsonb_strip_nulls(v_act - 'op_id') THEN
                PERFORM precis_trust_refuse(
                    format('op_id %s was already recorded on %s %s with a '
                           'different payload', v_act ->> 'op_id', p_kind, p_id),
                    'a new act needs a new op_id');
            END IF;
            RETURN ARRAY[p_old_origin, p_old_conf, p_old_version::text];
        END IF;
        IF p_semantic THEN
            PERFORM precis_trust_refuse(
                format('act %s cannot ride on a content change of %s %s',
                       v_action, p_kind, p_id),
                'commit the edit first, then record the act against the new sha');
        END IF;
        IF coalesce(v_act ->> 'expected_sha', '') = '' THEN
            PERFORM precis_trust_refuse(
                format('act %s on %s %s needs expected_sha', v_action, p_kind, p_id),
                'pass the content sha the act was decided on');
        END IF;
        IF v_act ->> 'expected_sha' IS DISTINCT FROM p_old_sha THEN
            PERFORM precis_trust_refuse(
                format('stale act: %s %s is at sha %s, the act expected %s',
                       p_kind, p_id, p_old_sha, v_act ->> 'expected_sha'),
                're-read the target and decide again on its current content');
        END IF;
        IF v_action = 'consider' THEN
            -- consideration does not reconstruct origin: unknown stays unknown
            RETURN ARRAY[p_old_origin, 'considered', '1'];
        ELSIF v_action = 'demote' THEN
            RETURN ARRAY[p_old_origin, coalesce(v_act ->> 'confidence', 'hunch'), '1'];
        ELSIF v_action = 'correct-origin' THEN
            RETURN ARRAY[v_act ->> 'origin',
                         CASE WHEN p_old_version = 0 THEN 'unknown' ELSE p_old_conf END,
                         '1'];
        ELSIF v_action = 'legacy-import' THEN
            IF p_old_version = 1 THEN
                PERFORM precis_trust_refuse(
                    format('legacy-import on %s %s, which is already stamped', p_kind, p_id),
                    'legacy-import only stamps version-0 rows');
            END IF;
            RETURN ARRAY[coalesce(v_act ->> 'origin', 'unknown'),
                         coalesce(v_act ->> 'confidence', 'unknown'), '1'];
        END IF;
        PERFORM precis_trust_refuse(
            format('act %s cannot apply to an existing %s', v_action, p_kind),
            'carry is a create-time act');
    END IF;

    IF v_origin IS NULL THEN
        IF p_insert THEN
            IF precis_trust_strict() THEN
                PERFORM precis_trust_refuse(
                    format('strict mode: new %s without a trust write context', p_kind),
                    'every ref/link writer runs inside revision_context(trust_origin=...)');
            END IF;
            RETURN ARRAY['unknown', 'unknown', '0'];
        END IF;
        IF p_semantic AND p_old_version = 1 THEN
            PERFORM precis_trust_refuse(
                format('%s %s is stamped %s/%s (version 1); a writer without a '
                       'trust context cannot change its content',
                       p_kind, p_id, p_old_origin, p_old_conf),
                'wrap this writer in revision_context(trust_origin=...) '
                '(docs/backlog/graph-node-trust-state.md, slice 1)');
        END IF;
        IF p_semantic AND precis_trust_strict() THEN
            PERFORM precis_trust_refuse(
                format('strict mode: %s %s changed without a trust write context',
                       p_kind, p_id),
                'every ref/link writer runs inside revision_context(trust_origin=...)');
        END IF;
        RETURN ARRAY[p_old_origin, p_old_conf, p_old_version::text];
    END IF;

    IF p_insert OR p_semantic THEN
        RETURN ARRAY[v_origin, 'hunch', '1'];
    END IF;
    RETURN ARRAY[p_old_origin, p_old_conf, p_old_version::text];
END
$$;

CREATE OR REPLACE FUNCTION precis_trust_refs_guard()
    RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    v text[];
    v_sem boolean := false;
    v_sha text;
    v_conv boolean := precis_trust_kind_converted(NEW.kind) OR precis_trust_strict();
BEGIN
    IF TG_OP = 'INSERT' THEN
        v := precis_trust_decide('ref', NEW.ref_id, true, NEW.kind,
            NULL, NULL, NULL,
            NEW.trust_origin, NEW.trust_confidence, NEW.trust_write_version,
            false, NULL, v_conv);
    ELSE
        -- a v1 row stays converted whatever its kind becomes
        v_conv := v_conv OR OLD.trust_write_version = 1;
        v_sem := precis_trust_ref_semantic(OLD, NEW);
        -- the sha is only needed by a targeted act; skip the body hash otherwise
        IF precis_revision_setting('trust_act') IS NOT NULL THEN
            v_sha := precis_ref_sha_of(OLD, precis_body_sha(OLD.ref_id));
        END IF;
        v := precis_trust_decide('ref', NEW.ref_id, false, NEW.kind,
            OLD.trust_origin, OLD.trust_confidence, OLD.trust_write_version,
            NEW.trust_origin, NEW.trust_confidence, NEW.trust_write_version,
            v_sem, v_sha, v_conv);
    END IF;
    NEW.trust_origin := v[1];
    NEW.trust_confidence := v[2];
    NEW.trust_write_version := v[3]::smallint;
    RETURN NEW;
END
$$;

CREATE OR REPLACE FUNCTION precis_trust_links_guard()
    RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    v text[];
    v_conv boolean;
BEGIN
    IF TG_OP = 'INSERT' THEN
        -- the ends are looked up only when a context or act could apply
        v_conv := (precis_revision_setting('trust_origin') IS NOT NULL
                   OR precis_revision_setting('trust_act') IS NOT NULL)
              AND precis_trust_link_converted(ARRAY[NEW.src_ref_id, NEW.dst_ref_id]);
        v := precis_trust_decide('link', NEW.link_id, true, NEW.relation,
            NULL, NULL, NULL,
            NEW.trust_origin, NEW.trust_confidence, NEW.trust_write_version,
            false, NULL, v_conv);
    ELSE
        -- a v1 link stays converted even if an end moves off the converted
        -- kinds, so a context-less writer cannot change it
        v_conv := OLD.trust_write_version = 1
              OR ((precis_revision_setting('trust_origin') IS NOT NULL
                   OR precis_revision_setting('trust_act') IS NOT NULL)
                  AND precis_trust_link_converted(ARRAY[OLD.src_ref_id, OLD.dst_ref_id,
                                                        NEW.src_ref_id, NEW.dst_ref_id]));
        v := precis_trust_decide('link', NEW.link_id, false, NEW.relation,
            OLD.trust_origin, OLD.trust_confidence, OLD.trust_write_version,
            NEW.trust_origin, NEW.trust_confidence, NEW.trust_write_version,
            precis_link_content(OLD) IS DISTINCT FROM precis_link_content(NEW),
            precis_link_sha_of(OLD), v_conv);
    END IF;
    NEW.trust_origin := v[1];
    NEW.trust_confidence := v[2];
    NEW.trust_write_version := v[3]::smallint;
    RETURN NEW;
END
$$;

-- ── recording acts (AFTER triggers, the body guard) ────────────────────

CREATE OR REPLACE FUNCTION precis_trust_append_act(
    p_kind text, p_id bigint, p_act jsonb, p_prev_sha text, p_prev_state jsonb)
    RETURNS void LANGUAGE plpgsql AS $$
DECLARE
    v_last jsonb;
BEGIN
    -- a body replace is DELETE + INSERT and a row-level guard fires once per
    -- chunk: consecutive identical acts in one transaction record once, and
    -- the first keeps the prior state
    SELECT trust_acts -> -1 INTO v_last
      FROM revisions
     WHERE target_kind = p_kind AND target_id = p_id AND xact = txid_current();
    IF v_last IS NOT NULL
       AND (v_last - 'at' - 'before') = (p_act - 'at' - 'before') THEN
        RETURN;
    END IF;
    INSERT INTO revisions AS r
        (target_kind, target_id, event, actor, model, reason,
         prev_sha, prev_state, trust_acts)
    VALUES (p_kind, p_id, 'trust-recorded',
            coalesce(precis_revision_setting('actor'), session_user::text),
            precis_revision_setting('model'),
            coalesce(precis_revision_setting('reason'), p_act ->> 'reason', '(unrecorded)'),
            p_prev_sha, coalesce(p_prev_state, '{}'::jsonb), jsonb_build_array(p_act))
    ON CONFLICT (target_kind, target_id, xact) DO UPDATE SET
        trust_acts = r.trust_acts || EXCLUDED.trust_acts;
END
$$;

CREATE OR REPLACE FUNCTION precis_trust_act_json(
    p_kind text, p_id bigint, p_action text, p_act jsonb,
    p_before text[], p_after text[], p_expected_sha text)
    RETURNS jsonb LANGUAGE sql STABLE AS $$
    SELECT jsonb_strip_nulls(jsonb_build_object(
        'op_id', coalesce(p_act ->> 'op_id',
                          p_action || ':' || txid_current()::text),
        'action', p_action,
        'target_kind', p_kind,
        'target_id', p_id,
        'expected_sha', p_expected_sha,
        'before', CASE WHEN p_before IS NULL THEN NULL ELSE jsonb_build_object(
            'origin', p_before[1], 'confidence', p_before[2],
            'version', p_before[3]::int) END,
        'after', jsonb_build_object(
            'origin', p_after[1], 'confidence', p_after[2],
            'version', p_after[3]::int),
        'actor', coalesce(precis_revision_setting('actor'), session_user::text),
        'actor_type', coalesce(precis_revision_setting('trust_actor_type'), 'unknown'),
        'model', precis_revision_setting('model'),
        'run', precis_revision_setting('trust_run'),
        'reason', coalesce(p_act ->> 'reason', precis_revision_setting('reason')),
        'payload', CASE WHEN p_act IS NULL THEN NULL ELSE p_act - 'op_id' END,
        'at', to_char(clock_timestamp() AT TIME ZONE 'UTC',
                      'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')))
$$;

CREATE OR REPLACE FUNCTION precis_trust_refs_act()
    RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    v_act jsonb := precis_trust_ctx_act();
    v_action text;
    v_before text[];
    v_sha text;
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF v_act IS NOT NULL AND v_act ->> 'target_id' IS NULL
           AND v_act ->> 'target_kind' = 'ref'
           AND precis_revision_setting('trust_act_bound') = NEW.ref_id::text THEN
            v_action := v_act ->> 'action';
        ELSE
            v_action := 'create';
            v_act := NULL;
        END IF;
    ELSE
        IF precis_revision_setting('trust_internal') = 'on' THEN
            RETURN NULL;
        END IF;
        v_before := ARRAY[OLD.trust_origin, OLD.trust_confidence,
                          OLD.trust_write_version::text];
        IF precis_trust_act_targets(v_act, 'ref', NEW.ref_id, false, NULL) THEN
            IF precis_trust_recorded_act('ref', NEW.ref_id, v_act ->> 'op_id') IS NOT NULL THEN
                RETURN NULL;
            END IF;
            v_action := v_act ->> 'action';
            v_sha := v_act ->> 'expected_sha';
        ELSIF precis_trust_ctx_origin() IS NOT NULL
              AND precis_trust_ref_semantic(OLD, NEW) THEN
            v_action := 'revise';
            v_act := NULL;
            v_sha := precis_ref_sha_of(OLD, precis_body_sha(OLD.ref_id));
        ELSE
            RETURN NULL;
        END IF;
    END IF;
    PERFORM precis_trust_append_act('ref', NEW.ref_id,
        precis_trust_act_json('ref', NEW.ref_id, v_action, v_act, v_before,
            ARRAY[NEW.trust_origin, NEW.trust_confidence,
                  NEW.trust_write_version::text], v_sha),
        v_sha,
        CASE WHEN TG_OP = 'UPDATE' THEN to_jsonb(OLD) END);
    RETURN NULL;
END
$$;

CREATE OR REPLACE FUNCTION precis_trust_links_act()
    RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    v_act jsonb := precis_trust_ctx_act();
    v_action text;
    v_before text[];
    v_sha text;
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF v_act IS NOT NULL AND v_act ->> 'target_id' IS NULL
           AND v_act ->> 'target_kind' = 'link'
           AND precis_revision_setting('trust_act_bound') = NEW.link_id::text THEN
            v_action := v_act ->> 'action';
        ELSE
            v_action := 'create';
            v_act := NULL;
        END IF;
    ELSE
        v_before := ARRAY[OLD.trust_origin, OLD.trust_confidence,
                          OLD.trust_write_version::text];
        IF precis_trust_act_targets(v_act, 'link', NEW.link_id, false, NULL) THEN
            IF precis_trust_recorded_act('link', NEW.link_id, v_act ->> 'op_id') IS NOT NULL THEN
                RETURN NULL;
            END IF;
            v_action := v_act ->> 'action';
            v_sha := v_act ->> 'expected_sha';
        ELSIF precis_trust_ctx_origin() IS NOT NULL
              AND precis_link_content(OLD) IS DISTINCT FROM precis_link_content(NEW) THEN
            v_action := 'revise';
            v_act := NULL;
            v_sha := precis_link_sha_of(OLD);
        ELSE
            RETURN NULL;
        END IF;
    END IF;
    PERFORM precis_trust_append_act('link', NEW.link_id,
        precis_trust_act_json('link', NEW.link_id, v_action, v_act, v_before,
            ARRAY[NEW.trust_origin, NEW.trust_confidence,
                  NEW.trust_write_version::text], v_sha),
        v_sha,
        CASE WHEN TG_OP = 'UPDATE' THEN to_jsonb(OLD) END);
    RETURN NULL;
END
$$;

-- A live body change (ord >= 0) on refs that existed before this
-- transaction. Unconverted writer: refused on a v1 ref (and on any ref in
-- strict mode). Converted writer: the ref is re-stamped <origin>/hunch v1
-- and a 'revise' act with "via": "body" is recorded.
CREATE OR REPLACE FUNCTION precis_trust_body_guard(p_ids bigint[])
    RETURNS void LANGUAGE plpgsql AS $$
DECLARE
    v_origin text;
    v_strict boolean;
    v record;
BEGIN
    IF p_ids IS NULL OR cardinality(p_ids) = 0 THEN
        RETURN;
    END IF;
    v_origin := precis_trust_ctx_origin();
    v_strict := precis_trust_strict();
    FOR v IN
        SELECT ref_id, kind, trust_origin, trust_confidence, trust_write_version
          FROM refs
         WHERE ref_id = ANY (p_ids) AND created_at < now()
         ORDER BY ref_id
    LOOP
        IF v_origin IS NULL
           OR NOT (v_strict OR v.trust_write_version = 1
                   OR precis_trust_kind_converted(v.kind)) THEN
            IF v.trust_write_version = 1 OR v_strict THEN
                PERFORM precis_trust_refuse(
                    format('the body of ref %s (stamped %s/%s, version %s) changed '
                           'without a trust write context',
                           v.ref_id, v.trust_origin, v.trust_confidence,
                           v.trust_write_version),
                    'wrap this writer in revision_context(trust_origin=...) '
                    '(docs/backlog/graph-node-trust-state.md, slice 1)');
            END IF;
            CONTINUE;
        END IF;
        PERFORM set_config('precis.trust_internal', 'on', true);
        UPDATE refs SET trust_origin = v_origin, trust_confidence = 'hunch',
                        trust_write_version = 1
         WHERE ref_id = v.ref_id;
        PERFORM set_config('precis.trust_internal', '', true);
        PERFORM precis_trust_append_act('ref', v.ref_id,
            precis_trust_act_json('ref', v.ref_id, 'revise', NULL,
                ARRAY[v.trust_origin, v.trust_confidence,
                      v.trust_write_version::text],
                ARRAY[v_origin, 'hunch', '1'], NULL) || '{"via": "body"}'::jsonb,
            NULL, NULL);
    END LOOP;
END
$$;

-- Is a live chunk of this kind part of its ref's trust subject? Appended
-- timeline entries are not: a comment, a job event/summary/result, a run
-- or quest log line, a review note, a figure/diagram chat turn is another
-- producer's separate entry on the ref (recorded in the revisions log as
-- before), not a rewrite of the ref's own assertion, so it neither needs
-- the owner's context nor re-stamps it. A tag_overflow chunk is a long tag
-- value's storage (handlers/_tag_redirect.py), not body. An explicit list:
-- a new chunk kind is part of the subject until it is classified here.
CREATE OR REPLACE FUNCTION precis_trust_subject_chunk(p_chunk_kind text, p_ord integer)
    RETURNS boolean LANGUAGE sql IMMUTABLE AS $$
    SELECT p_ord >= 0 AND NOT p_chunk_kind = ANY (ARRAY[
        'gripe_comment', 'todo_comment', 'issue_comment', 'job_event',
        'job_summary', 'job_result', 'run_log', 'quest_log', 'llm_review',
        'figure_turn', 'mermaid_turn', 'tag_overflow'])
$$;

CREATE OR REPLACE FUNCTION precis_trust_chunks_ins()
    RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM precis_trust_body_guard(
        (SELECT array_agg(DISTINCT ref_id) FROM new_rows
          WHERE precis_trust_subject_chunk(chunk_kind, ord)));
    RETURN NULL;
END
$$;

CREATE OR REPLACE FUNCTION precis_trust_chunks_del()
    RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM precis_trust_body_guard(
        (SELECT array_agg(DISTINCT ref_id) FROM old_rows
          WHERE precis_trust_subject_chunk(chunk_kind, ord) AND retired_at IS NULL));
    RETURN NULL;
END
$$;

CREATE OR REPLACE FUNCTION precis_trust_chunks_upd()
    RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (OLD.ref_id, OLD.ord, OLD.text, OLD.retired_at)
           IS NOT DISTINCT FROM (NEW.ref_id, NEW.ord, NEW.text, NEW.retired_at) THEN
        RETURN NULL;
    END IF;
    PERFORM precis_trust_body_guard(ARRAY(
        SELECT DISTINCT x FROM unnest(ARRAY[
            CASE WHEN precis_trust_subject_chunk(OLD.chunk_kind, OLD.ord)
                      AND OLD.retired_at IS NULL THEN OLD.ref_id END,
            CASE WHEN precis_trust_subject_chunk(NEW.chunk_kind, NEW.ord)
                      AND NEW.retired_at IS NULL THEN NEW.ref_id END]) x
         WHERE x IS NOT NULL));
    RETURN NULL;
END
$$;

-- ── 0185 replacements ──────────────────────────────────────────────────

-- As 0185, plus: a content event outranks a 'trust-recorded' row, and a
-- trust row without a prior state takes the content event's.
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
        event = CASE WHEN r.event IN ('edited', 'trust-recorded')
                     THEN EXCLUDED.event ELSE r.event END,
        prev_sha = coalesce(r.prev_sha, EXCLUDED.prev_sha),
        prev_state = CASE
            WHEN r.prev_state = '{}'::jsonb THEN EXCLUDED.prev_state
            WHEN EXCLUDED.prev_state ? 'chunks' AND NOT r.prev_state ? 'chunks'
            THEN r.prev_state || jsonb_build_object('chunks', EXCLUDED.prev_state -> 'chunks')
            ELSE r.prev_state END;
END
$$;

-- As 0185, plus: a row carrying trust acts is never dropped as a no-op
-- (edit -> consider -> undo keeps every act), and a 'create' act whose
-- target was not created in this transaction (an upsert that hit an
-- existing row) is dropped as a phantom.
CREATE OR REPLACE FUNCTION precis_revisions_seal()
    RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    v_event text;
    v_prev text;
    v_acts jsonb;
    v_now text;
    v_created boolean;
BEGIN
    BEGIN
        SELECT event, prev_sha, trust_acts INTO v_event, v_prev, v_acts
          FROM revisions WHERE revision_id = NEW.revision_id;
        IF NOT FOUND THEN
            RETURN NULL;
        END IF;
        IF jsonb_array_length(v_acts) > 0 THEN
            IF NEW.target_kind = 'ref' THEN
                SELECT created_at = now() INTO v_created
                  FROM refs WHERE ref_id = NEW.target_id;
            ELSE
                SELECT created_at = now() INTO v_created
                  FROM links WHERE link_id = NEW.target_id;
            END IF;
            IF NOT coalesce(v_created, false) THEN
                SELECT coalesce(jsonb_agg(a ORDER BY o), '[]'::jsonb) INTO v_acts
                  FROM jsonb_array_elements(v_acts) WITH ORDINALITY AS t(a, o)
                 WHERE a -> 'before' IS NOT NULL;
            END IF;
        END IF;
        v_now := precis_target_sha(NEW.target_kind, NEW.target_id);
        IF jsonb_array_length(v_acts) = 0
           AND (v_event = 'trust-recorded'
                OR (v_event = 'edited' AND v_now IS NOT DISTINCT FROM v_prev)) THEN
            DELETE FROM revisions WHERE revision_id = NEW.revision_id;
        ELSE
            UPDATE revisions SET new_sha = v_now, trust_acts = v_acts
             WHERE revision_id = NEW.revision_id;
        END IF;
    EXCEPTION WHEN OTHERS THEN
        RAISE WARNING 'revisions: seal of % not done: %', NEW.revision_id, SQLERRM;
    END;
    RETURN NULL;
END
$$;

-- ── strict switch (coordinator-run, never by this migration) ────────────
--
-- Takes SHARE ROW EXCLUSIVE on refs, links and chunks, which conflicts with
-- every writer's ROW EXCLUSIVE: transactions already writing finish first,
-- later ones queue behind the switch and see strict on their next
-- statement. lock_timeout bounds the wait; a timeout aborts the switch and
-- leaves compat in force. One-way: there is no function back to compat.
CREATE OR REPLACE FUNCTION precis_trust_activate_strict(p_lock_timeout text DEFAULT '5s')
    RETURNS timestamptz LANGUAGE plpgsql AS $$
DECLARE
    v_at timestamptz;
BEGIN
    PERFORM set_config('lock_timeout', p_lock_timeout, true);
    LOCK TABLE refs, links, chunks IN SHARE ROW EXCLUSIVE MODE;
    INSERT INTO trust_write_state (singleton, mode, strict_at)
    VALUES (true, 'strict', now())
    ON CONFLICT (singleton) DO UPDATE
        SET mode = 'strict',
            strict_at = coalesce(trust_write_state.strict_at, EXCLUDED.strict_at)
    RETURNING strict_at INTO v_at;
    RETURN v_at;
END
$$;

-- ── from here on: statements that lock refs / links / chunks / revisions ─
--
-- Lock order follows the live writers: they lock refs, links or chunks
-- first and reach revisions through the logging triggers, so revisions is
-- locked last (locking it first could deadlock against a writer holding a
-- refs row lock). The migration runner sets lock_timeout = 0; this file
-- bounds the wait instead, so a busy table fails the apply (nothing
-- committed, retry in a quiet window) rather than queueing every reader
-- behind an ACCESS EXCLUSIVE request.
SET LOCAL lock_timeout = '10s';
LOCK TABLE refs, links, chunks, revisions IN ACCESS EXCLUSIVE MODE;

ALTER TABLE refs
    ADD COLUMN IF NOT EXISTS trust_origin text NOT NULL DEFAULT 'unknown',
    ADD COLUMN IF NOT EXISTS trust_confidence text NOT NULL DEFAULT 'unknown',
    -- 0 or 1 (CHECK below): smallint is the range
    -- squawk-ignore prefer-bigint-over-smallint
    ADD COLUMN IF NOT EXISTS trust_write_version smallint NOT NULL DEFAULT 0;
ALTER TABLE links
    ADD COLUMN IF NOT EXISTS trust_origin text NOT NULL DEFAULT 'unknown',
    ADD COLUMN IF NOT EXISTS trust_confidence text NOT NULL DEFAULT 'unknown',
    -- 0 or 1 (CHECK below): smallint is the range
    -- squawk-ignore prefer-bigint-over-smallint
    ADD COLUMN IF NOT EXISTS trust_write_version smallint NOT NULL DEFAULT 0;

ALTER TABLE refs ADD CONSTRAINT refs_trust_state_check CHECK (
    trust_origin IN ('human', 'extracted', 'computed', 'generated', 'unknown')
    AND trust_confidence IN ('hunch', 'considered', 'unknown')
    AND trust_write_version IN (0, 1)
    AND (trust_write_version = 1
         OR (trust_origin = 'unknown' AND trust_confidence = 'unknown'))) NOT VALID;
ALTER TABLE links ADD CONSTRAINT links_trust_state_check CHECK (
    trust_origin IN ('human', 'extracted', 'computed', 'generated', 'unknown')
    AND trust_confidence IN ('hunch', 'considered', 'unknown')
    AND trust_write_version IN (0, 1)
    AND (trust_write_version = 1
         OR (trust_origin = 'unknown' AND trust_confidence = 'unknown'))) NOT VALID;

COMMENT ON COLUMN refs.trust_origin IS
    'Who/how this ref was made: human | extracted | computed | generated | '
    'unknown (legacy). Set by the write context, never by the statement '
    '(0214). Not a quality score; set_by and meta.origin keep their meanings.';
COMMENT ON COLUMN refs.trust_confidence IS
    'hunch | considered | unknown (legacy). considered only through a recorded '
    'consider act (revisions.trust_acts).';
COMMENT ON COLUMN refs.trust_write_version IS
    '0 = unstamped legacy or compatibility write (unknown/unknown); 1 = '
    'written under the slice-1 trust contract. Not a trust rank.';
COMMENT ON COLUMN links.trust_origin IS 'As refs.trust_origin (0214).';
COMMENT ON COLUMN links.trust_confidence IS 'As refs.trust_confidence (0214).';
COMMENT ON COLUMN links.trust_write_version IS 'As refs.trust_write_version (0214).';

DROP TRIGGER IF EXISTS refs_trust_guard ON refs;
CREATE TRIGGER refs_trust_guard
    BEFORE INSERT OR UPDATE ON refs FOR EACH ROW
    EXECUTE FUNCTION precis_trust_refs_guard();

DROP TRIGGER IF EXISTS refs_trust_act ON refs;
CREATE TRIGGER refs_trust_act
    AFTER INSERT OR UPDATE ON refs FOR EACH ROW
    WHEN (NEW.trust_write_version = 1)
    EXECUTE FUNCTION precis_trust_refs_act();

DROP TRIGGER IF EXISTS links_trust_guard ON links;
CREATE TRIGGER links_trust_guard
    BEFORE INSERT OR UPDATE ON links FOR EACH ROW
    EXECUTE FUNCTION precis_trust_links_guard();

DROP TRIGGER IF EXISTS links_trust_act ON links;
CREATE TRIGGER links_trust_act
    AFTER INSERT OR UPDATE ON links FOR EACH ROW
    WHEN (NEW.trust_write_version = 1)
    EXECUTE FUNCTION precis_trust_links_act();

DROP TRIGGER IF EXISTS chunks_trust_body_insert ON chunks;
CREATE TRIGGER chunks_trust_body_insert
    AFTER INSERT ON chunks REFERENCING NEW TABLE AS new_rows
    FOR EACH STATEMENT EXECUTE FUNCTION precis_trust_chunks_ins();

DROP TRIGGER IF EXISTS chunks_trust_body_delete ON chunks;
CREATE TRIGGER chunks_trust_body_delete
    AFTER DELETE ON chunks REFERENCING OLD TABLE AS old_rows
    FOR EACH STATEMENT EXECUTE FUNCTION precis_trust_chunks_del();

DROP TRIGGER IF EXISTS chunks_trust_body_update ON chunks;
CREATE TRIGGER chunks_trust_body_update
    AFTER UPDATE OF ref_id, ord, text, retired_at ON chunks FOR EACH ROW
    EXECUTE FUNCTION precis_trust_chunks_upd();

ALTER TABLE revisions
    ADD COLUMN IF NOT EXISTS trust_acts jsonb NOT NULL DEFAULT '[]'::jsonb;

COMMENT ON COLUMN revisions.trust_acts IS
    'Ordered trust acts (graph-node-trust-state slice 1) recorded on this '
    'target in this transaction: op_id, action (create, revise, consider, '
    'demote, correct-origin, legacy-import, carry), before/after '
    'origin+confidence+version, actor, actor_type, model, run, reason, '
    'expected_sha, payload, at (UTC). Appended by triggers only.';

-- Widened, not removed: re-added on the next line with 'trust-recorded'.
-- squawk-ignore ban-drop-constraint
ALTER TABLE revisions DROP CONSTRAINT IF EXISTS revisions_event_check;
ALTER TABLE revisions ADD CONSTRAINT revisions_event_check CHECK (event IN (
    'edited', 'retired', 'restored', 'deleted', 'merged-into', 'trust-recorded'))
    NOT VALID;
ALTER TABLE revisions ADD CONSTRAINT revisions_trust_acts_array
    CHECK (jsonb_typeof(trust_acts) = 'array') NOT VALID;

COMMIT;
