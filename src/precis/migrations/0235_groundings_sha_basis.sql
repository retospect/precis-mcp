-- 0235_groundings_sha_basis.sql
--
-- Graph trust state, slice 2b (docs/backlog/graph-node-trust-state.md;
-- Reto rulings 2026-10-21 on the trust2b-memo). Four pieces:
--
--   * groundings: a ref's or link's CURRENT typed grounding — defined |
--     sourced | verified; motivated and none stay derived. Current state
--     lives here, never in meta (a covered meta write would stale the very
--     review certifying it; an uncovered one would carry no history). A
--     defined grounding is never upgraded in place: a later sourced or
--     verified grounding is a sibling row, and demotion is explicit
--     (retired_at / retired_reason) — nothing is deleted. History is
--     'ground'/'unground' acts appended to the target's
--     revisions.trust_acts by this table's own AFTER triggers, so SQL and
--     Python writes are recorded identically.
--
--   * reviews.sha_basis: 'content' (default; every existing row, the 0185
--     precis_target_sha semantics) or 'trust-subject' (2b rows). Currency
--     dispatches per row — Python compares sha_basis='trust-subject'
--     against precis_trust_subject_sha. reviews.target_kind gains
--     'grounding' so a review certifies one grounding row.
--
--   * the trust-subject sha: precis_trust_body_sha hashes only the chunks
--     precis_trust_subject_chunk names (0214's exclusions — comments, job
--     events/summaries/results, run/quest logs, review notes, figure turns,
--     tag overflow are a producer's own timeline entries, not the ref's
--     assertion), so a comment no longer stales a certificate.
--     precis_trust_subject_sha('ref') is that body sha inside
--     precis_ref_sha_of; a link has no timeline chunks so its subject sha
--     stays precis_link_sha_of. A grounding's sha
--     (precis_trust_subject_sha('grounding', id)) binds the target's
--     subject sha, the row's own content (type + pins) and each pinned
--     dependency's current sha — a source edit stales the certificate at
--     read time with no write path needing to remember. 0185's
--     precis_target_sha and the revisions prev_sha/new_sha chain keep
--     their content-sha meaning untouched.
--
--   * 'admit' joins the act vocabulary (Q6): a targeted act through the
--     same 0214 machinery — the four admission conditions are a
--     Python-side gate checked in one transaction; recorded by triggers
--     like the other acts. The targeted-act staleness check moves from the
--     content sha to the subject sha (precis_trust_refs_guard /
--     precis_trust_links_guard and precis_trust_decide are OR REPLACE'd
--     here, the sealed file untouched), so an appended timeline entry
--     cannot stale a pending consider/admit either.
--
-- Q2 LAX (the 2026-10-21 ruling): the review actor is recorded as the
-- caller states it — no DB role, ingress boundary or login allowlist is
-- built here; the role-behind-ingress design stays the target.
--
-- Forward-only (ADR 0005). The groundings table and functions take no
-- heavy locks; the reviews/revisions DDL comes last under the same
-- lock_timeout pattern as 0214.

BEGIN;

-- ── the table ──────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS groundings (
    grounding_id   bigserial PRIMARY KEY,
    target_kind    text NOT NULL CHECK (target_kind IN ('ref', 'link')),
    target_id      bigint NOT NULL,
    grounding      text NOT NULL CHECK (grounding IN ('defined', 'sourced', 'verified')),
    -- [{handle, kind: 'ref'|'chunk'|NULL, id}] — resolved at write; a pin
    -- that is not an addressable handle keeps kind NULL and binds by its
    -- literal (a dead handle still stales: 'gone:<handle>').
    pins           jsonb NOT NULL DEFAULT '[]'::jsonb
                   CHECK (jsonb_typeof(pins) = 'array'),
    -- for 'defined': the target's trust-subject sha when defined — the
    -- text the human review certified.
    definition_sha text,
    -- the certifying reviews row (for 'defined' an approved owner review;
    -- stored, not enforced by FK cascade semantics — reviews are audit).
    review_id      bigint REFERENCES reviews (review_id),
    recorded_by    text NOT NULL CHECK (btrim(recorded_by) <> ''),
    actor_type     text NOT NULL,
    at             timestamptz NOT NULL DEFAULT now(),
    retired_at     timestamptz,
    retired_reason text,
    CHECK (grounding = 'defined' OR jsonb_array_length(pins) > 0),
    CHECK (grounding <> 'defined' OR definition_sha IS NOT NULL)
);

COMMENT ON TABLE groundings IS
    'Current typed grounding of a ref or link (graph-node-trust-state '
    'slice 2b): defined (human-reviewed definition), sourced (pinned '
    'passage/finding), verified (checker-confirmed fidelity); motivated '
    'and none stay derived. Never in-place upgrade — siblings per type; '
    'demotion via retired_at/retired_reason. History: ground/unground acts '
    'in revisions.trust_acts.';

CREATE INDEX IF NOT EXISTS groundings_target_live_idx
    ON groundings (target_kind, target_id) WHERE retired_at IS NULL;
CREATE INDEX IF NOT EXISTS groundings_review_idx ON groundings (review_id);

-- ── the shas ───────────────────────────────────────────────────────────

-- Like precis_body_sha but over the ref's trust subject only: live
-- ord >= 0 chunks whose kind is not a listed timeline kind.
CREATE OR REPLACE FUNCTION precis_trust_body_sha(p_ref_id bigint)
    RETURNS text LANGUAGE sql STABLE AS $$
    SELECT md5(coalesce(string_agg(md5(c.text), ',' ORDER BY c.ord, c.chunk_id), ''))
      FROM chunks c
     WHERE c.ref_id = p_ref_id AND c.retired_at IS NULL
       AND precis_trust_subject_chunk(c.chunk_kind, c.ord)
$$;

-- One grounding row's certificate sha: its own content (target, type,
-- pins, definition_sha), the target's trust-subject sha, and every pinned
-- dependency's current sha. NULL for a retired or missing row — a retired
-- grounding's reviews stop being current.
CREATE OR REPLACE FUNCTION precis_grounding_sha(p_id bigint)
    RETURNS text LANGUAGE plpgsql STABLE AS $$
DECLARE
    g record;
    v_pins text;
BEGIN
    SELECT * INTO g FROM groundings WHERE grounding_id = p_id;
    IF NOT FOUND OR g.retired_at IS NOT NULL THEN
        RETURN NULL;
    END IF;
    SELECT string_agg(coalesce(
               CASE p.pin ->> 'kind'
                   WHEN 'ref' THEN (SELECT precis_trust_subject_sha('ref', r.ref_id)
                                      FROM refs r
                                     WHERE r.ref_id = (p.pin ->> 'id')::bigint
                                       AND r.retired_at IS NULL)
                   WHEN 'chunk' THEN (SELECT precis_target_sha('chunk', ch.chunk_id)
                                        FROM chunks ch
                                       WHERE ch.chunk_id = (p.pin ->> 'id')::bigint
                                         AND ch.retired_at IS NULL)
                   ELSE NULL END,
               'gone:' || (p.pin ->> 'handle')),
               ',' ORDER BY p.o)
      INTO v_pins
      FROM jsonb_array_elements(g.pins) WITH ORDINALITY AS p(pin, o);
    RETURN left(md5(concat_ws('|',
        g.target_kind, g.target_id::text, g.grounding, g.pins::text,
        coalesce(g.definition_sha, ''),
        coalesce(precis_trust_subject_sha(g.target_kind, g.target_id), '(gone)'),
        coalesce(v_pins, '(none)'))), 16);
END
$$;

-- The sha a certificate binds (2b): ref = kind+meta projection + subject
-- body; link = its content; grounding = precis_grounding_sha.
CREATE OR REPLACE FUNCTION precis_trust_subject_sha(p_kind text, p_id bigint)
    RETURNS text LANGUAGE plpgsql STABLE AS $$
DECLARE
    s text;
BEGIN
    IF p_kind = 'ref' THEN
        SELECT precis_ref_sha_of(r, precis_trust_body_sha(r.ref_id)) INTO s
          FROM refs r WHERE r.ref_id = p_id;
    ELSIF p_kind = 'link' THEN
        SELECT precis_link_sha_of(l) INTO s FROM links l WHERE l.link_id = p_id;
    ELSIF p_kind = 'grounding' THEN
        s := precis_grounding_sha(p_id);
    END IF;
    RETURN s;
END
$$;

-- The live grounding types on a target, optionally excluding one row —
-- the before/after sets the ground/unground acts record.
CREATE OR REPLACE FUNCTION precis_grounding_set(
    p_kind text, p_id bigint, p_exclude bigint DEFAULT NULL)
    RETURNS jsonb LANGUAGE sql STABLE AS $$
    SELECT coalesce(jsonb_agg(g.grounding ORDER BY g.grounding), '[]'::jsonb)
      FROM (SELECT DISTINCT g.grounding FROM groundings g
             WHERE g.target_kind = p_kind AND g.target_id = p_id
               AND g.retired_at IS NULL
               AND (p_exclude IS NULL OR g.grounding_id <> p_exclude)) g
$$;

-- ── ground/unground acts (AFTER trigger on groundings) ─────────────────

CREATE OR REPLACE FUNCTION precis_groundings_act()
    RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    v_action text;
    v_before jsonb;
    v_after jsonb;
BEGIN
    IF TG_OP = 'INSERT' THEN
        v_action := 'ground';
        v_before := jsonb_build_object('groundings',
            precis_grounding_set(NEW.target_kind, NEW.target_id, NEW.grounding_id));
        v_after := jsonb_build_object('groundings',
            precis_grounding_set(NEW.target_kind, NEW.target_id));
    ELSIF TG_OP = 'UPDATE'
          AND OLD.retired_at IS NULL AND NEW.retired_at IS NOT NULL THEN
        v_action := 'unground';
        v_before := jsonb_build_object('groundings',
            precis_grounding_set(NEW.target_kind, NEW.target_id)
                || jsonb_build_array(OLD.grounding));
        v_after := jsonb_build_object('groundings',
            precis_grounding_set(NEW.target_kind, NEW.target_id));
    ELSE
        RETURN NULL;
    END IF;
    -- prev_sha is the target's content sha: the revisions column stays a
    -- content-sha chain (the sealer's new_sha is precis_target_sha); the
    -- subject sha the grounding binds lives in the act's definition_sha /
    -- pins fields.
    PERFORM precis_trust_append_act(
        coalesce(NEW.target_kind, OLD.target_kind),
        coalesce(NEW.target_id, OLD.target_id),
        jsonb_strip_nulls(jsonb_build_object(
            'op_id', 'grounding:' || coalesce(NEW.grounding_id, OLD.grounding_id)
                     || ':' || v_action || ':' || txid_current()::text,
            'action', v_action,
            'target_kind', coalesce(NEW.target_kind, OLD.target_kind),
            'target_id', coalesce(NEW.target_id, OLD.target_id),
            'grounding_id', coalesce(NEW.grounding_id, OLD.grounding_id),
            'grounding', coalesce(NEW.grounding, OLD.grounding),
            'pins', CASE WHEN TG_OP = 'INSERT' THEN NEW.pins END,
            'review_id', coalesce(NEW.review_id, OLD.review_id),
            'definition_sha',
                CASE WHEN TG_OP = 'INSERT' THEN NEW.definition_sha END,
            'before', v_before,
            'after', v_after,
            'actor', coalesce(precis_revision_setting('actor'), session_user::text),
            'actor_type',
                coalesce(precis_revision_setting('trust_actor_type'), 'unknown'),
            'model', precis_revision_setting('model'),
            'reason',
                coalesce(precis_revision_setting('reason'), '(unrecorded)'),
            'at', to_char(clock_timestamp() AT TIME ZONE 'UTC',
                          'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'))),
        precis_target_sha(coalesce(NEW.target_kind, OLD.target_kind),
                          coalesce(NEW.target_id, OLD.target_id)),
        NULL);
    RETURN NULL;
END
$$;

DROP TRIGGER IF EXISTS groundings_trust_act ON groundings;
CREATE TRIGGER groundings_trust_act
    AFTER INSERT OR UPDATE ON groundings FOR EACH ROW
    EXECUTE FUNCTION precis_groundings_act();

-- ── 'admit' joins the act vocabulary ───────────────────────────────────

-- As 0214 plus 'admit'.
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
           'consider', 'demote', 'correct-origin', 'legacy-import', 'carry',
           'admit'])
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

-- As 0214, plus: 'admit' records without changing the stamp (admission is
-- its own axis — it changes no origin), and the targeted-act staleness
-- check compares expected_sha to the TRUST-SUBJECT sha (p_old_sha is now
-- supplied as that by the guards below) instead of the content sha.
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
                'pass the subject sha the act was decided on');
        END IF;
        IF v_act ->> 'expected_sha' IS DISTINCT FROM p_old_sha THEN
            PERFORM precis_trust_refuse(
                format('stale act: %s %s is at subject sha %s, the act expected %s',
                       p_kind, p_id, p_old_sha, v_act ->> 'expected_sha'),
                're-read the target and decide again on its current subject');
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
        ELSIF v_action = 'admit' THEN
            -- admission is its own axis: recorded, stamp unchanged
            RETURN ARRAY[p_old_origin, p_old_conf, '1'];
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

-- As 0214, but the targeted-act sha is the trust-SUBJECT sha: a comment or
-- log line appended to the ref no longer stales a pending act.
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
            v_sha := precis_trust_subject_sha('ref', OLD.ref_id);
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
            precis_trust_subject_sha('link', OLD.link_id), v_conv);
    END IF;
    NEW.trust_origin := v[1];
    NEW.trust_confidence := v[2];
    NEW.trust_write_version := v[3]::smallint;
    RETURN NEW;
END
$$;

-- ── reviews.sha_basis + 'grounding' target kind ────────────────────────

SET LOCAL lock_timeout = '10s';
LOCK TABLE refs, links, chunks, revisions, reviews IN ACCESS EXCLUSIVE MODE;

ALTER TABLE reviews
    ADD COLUMN IF NOT EXISTS sha_basis text NOT NULL DEFAULT 'content';
-- squawk-ignore ban-drop-constraint
ALTER TABLE reviews DROP CONSTRAINT IF EXISTS reviews_sha_basis_check;
ALTER TABLE reviews ADD CONSTRAINT reviews_sha_basis_check
    CHECK (sha_basis IN ('content', 'trust-subject')) NOT VALID;
-- reviews is a small ledger; a validating scan in the single-writer
-- window costs nothing (0187's pattern).
-- squawk-ignore constraint-missing-not-valid
ALTER TABLE reviews VALIDATE CONSTRAINT reviews_sha_basis_check;

-- squawk-ignore ban-drop-constraint
ALTER TABLE reviews DROP CONSTRAINT IF EXISTS reviews_target_kind_check;
ALTER TABLE reviews ADD CONSTRAINT reviews_target_kind_check
    CHECK (target_kind IN ('chunk', 'ref', 'link', 'measure', 'grounding'))
    NOT VALID;
-- squawk-ignore constraint-missing-not-valid
ALTER TABLE reviews VALIDATE CONSTRAINT reviews_target_kind_check;

COMMENT ON COLUMN reviews.sha_basis IS
    'Which sha content_sha binds: ''content'' = 0185 precis_target_sha '
    '(every row before 0235); ''trust-subject'' = precis_trust_subject_sha '
    '(excludes timeline chunks; grounding rows bind their own combined '
    'sha). Currency dispatch is per row (0235).';

COMMIT;
