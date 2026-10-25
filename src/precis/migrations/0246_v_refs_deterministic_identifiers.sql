-- 0246_v_refs_deterministic_identifiers.sql
--
-- gr516504, step 1 of 2 (approved interim): v_refs reads pub_id,
-- cite_key and paper_id through scalar subqueries, so a ref holding
-- more than one same-kind ref_identifiers row makes the view raise
-- "more than one row returned by a subquery" — on prod 946 refs carry
-- >1 pub_id row and 1128 carry >1 cite_key row, failing every read of
-- them. Nothing constrains (ref_id, id_kind) — the pk is
-- (id_kind, id_value) — and multi-row state is *legal*: a taproot
-- reword mints a fresh pub_id and keeps the old one so existing cites
-- keep resolving (Store.current_pub_ids), and the 2026-06 tex-import
-- minted a second, longer cite_key beside most early 'local' keys.
--
-- The view picks one row per kind deterministically so reads stop
-- failing; the canonical-row decision and the (ref_id, id_kind)
-- partial unique index are step 2, driven by the dry list in
-- scripts/v_refs_dup_identifiers.sql. The picks keep what each
-- surface already shows today:
--   * pub_id   — newest-minted wins (ORDER BY created_at DESC): the
--                same rule current_pub_ids uses for "the live
--                version", the value a fresh cites-edge stamps.
--   * cite_key — first-minted wins (ORDER BY created_at ASC): the
--                original key is the short 'local' one §-cites and
--                handle displays already resolve (the later
--                tex-import key stays resolvable — mentions.py
--                matches ANY cite_key row — it just isn't the one
--                shown).
--   * paper_id — first-minted, same as cite_key: content-derived at
--                ingest, so the original is the artifact's own id.
--   id_value is the stable tiebreak in each ORDER BY so an equal
--   created_at (a same-transaction bulk insert) can't drift.
--
-- Forward-only (ADR 0005). CREATE OR REPLACE — the column names,
-- order and types are identical to 0149's, so the swap is legal
-- without a DROP (squawk's ban-drop-view, and the view has no
-- dependents or application reader anyway; it exists for ad-hoc SQL).
-- Idempotent.

BEGIN;

CREATE OR REPLACE VIEW public.v_refs AS
 SELECT ref_id,
    kind,
    set_by,
    title,
    authors,
    year,
    provider,
    human_verified_at,
    human_verified_by,
    human_verified_note,
    retraction_status,
    retracted_at,
    retraction_reason,
    retraction_url,
    retraction_checked_at,
    pdf_sha256,
    pdf_pages,
    pdf_role,
    meta,
    retired_at,
    created_at,
    updated_at,
    ( SELECT ref_identifiers.id_value
           FROM public.ref_identifiers
          WHERE ((ref_identifiers.ref_id = r.ref_id) AND (ref_identifiers.id_kind = 'pub_id'::text))
          ORDER BY ref_identifiers.created_at DESC, ref_identifiers.id_value
          LIMIT 1) AS pub_id,
    ( SELECT ref_identifiers.id_value
           FROM public.ref_identifiers
          WHERE ((ref_identifiers.ref_id = r.ref_id) AND (ref_identifiers.id_kind = 'cite_key'::text))
          ORDER BY ref_identifiers.created_at, ref_identifiers.id_value
          LIMIT 1) AS cite_key,
    ( SELECT ref_identifiers.id_value
           FROM public.ref_identifiers
          WHERE ((ref_identifiers.ref_id = r.ref_id) AND (ref_identifiers.id_kind = 'paper_id'::text))
          ORDER BY ref_identifiers.created_at, ref_identifiers.id_value
          LIMIT 1) AS paper_id
   FROM public.refs r;

COMMIT;

-- End of 0246_v_refs_deterministic_identifiers.sql
