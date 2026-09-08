-- precis_se/0005_se_notes_freedom.sql
--
-- se slice 4, round 1 (se-kind.md "Ship order" step 4): the
-- propose/interrogate substrate — the ``se_notes`` ledger backing
-- ``view='interview'``, and the design-freedom vocabulary
-- (set-based-design research, perplexity-reasoning:310975: the human
-- declares invariants and acceptable SETS, the solver owns detail, the
-- system reports remaining freedom honestly):
--
-- * ``se_notes`` — the structured, linkable question/answer/decision
--   ledger. Name-keyed text throughout (``re`` names another note,
--   ``about`` anchors block / block.measure names) — never a row-id FK,
--   for the same reason as ``se_connects``/``template_ref``: block (and
--   note) row ids are rebuilt on every ``persist.save_tree``.
--   ``created_at`` is CARRIED by the save (COALESCE'd on reinsert) so the
--   interview timeline survives the retire-all/reinsert-all save model.
--   A question's open/settled state is DERIVED at read time (a live
--   answer/decision whose ``re`` names it settles it) — no status column
--   to drift.
--
-- * ``se_measures`` grows the freedom fields: an optional
--   ``min_value``/``max_value`` interval as the declarative alternative
--   to a point ``value`` ("bore ≥ 4 mm" is a set, a forced point is
--   overspecification); ``origin`` (user | proposed) — se_propose may
--   freely revise its own choices and must treat the user's as contract
--   (the RFdiffusion fixed-motif/free-scaffold split); ``unit`` from the
--   closed registry m | count | ratio | deg (views render per-unit;
--   relations require unit agreement; the relation's new ``scale``
--   factor is dimensionless and rides the existing jsonb column — no
--   DDL).
--
-- * ``se_blocks.origins`` — the same user|proposed stamp for the block's
--   authored facets, jsonb keyed by facet name ('envelope', 'pose');
--   an absent key means 'user' (the default is not stored).
--
-- Zero live rows in every se table in prod as of this migration.
--
-- Forward-only (ADR 0005). Idempotent. This is a PLUGIN migration
-- (namespace ``precis_se``), applied after 0001-0004.

BEGIN;

CREATE TABLE IF NOT EXISTS se_notes (
    id          bigserial PRIMARY KEY,
    ref_id      bigint NOT NULL,
    name        text   NOT NULL,
    kind        text   NOT NULL,
    body        text   NOT NULL,
    re          text,
    about       jsonb  NOT NULL DEFAULT '[]'::jsonb,
    origin      text   NOT NULL DEFAULT 'user',
    created_at  timestamptz NOT NULL DEFAULT now(),
    retired_at  timestamptz
);

COMMENT ON TABLE se_notes IS
    'se design interrogation ledger (se-kind.md slice 4): name-keyed '
    'question/answer/decision notes on a design. re names the note this '
    'one answers/decides; about anchors block / block.measure names '
    '(name-keyed text, resolved at read time — dangling anchors are a '
    'read-time report, never a write-time error). kind and origin are '
    'code-vetted vocabularies (question|answer|decision, user|proposed); '
    'a stored stray surfaces as a read-time finding, never a crash.';

COMMENT ON COLUMN se_notes.created_at IS
    'Carried across saves (persist reinserts with COALESCE) so the '
    'interview timeline survives the retire-all/reinsert-all save model.';

CREATE INDEX IF NOT EXISTS se_notes_ref_live_idx
    ON se_notes (ref_id) WHERE retired_at IS NULL;

ALTER TABLE se_measures ADD COLUMN IF NOT EXISTS min_value double precision;
ALTER TABLE se_measures ADD COLUMN IF NOT EXISTS max_value double precision;
ALTER TABLE se_measures ADD COLUMN IF NOT EXISTS origin text NOT NULL DEFAULT 'user';
ALTER TABLE se_measures ADD COLUMN IF NOT EXISTS unit text NOT NULL DEFAULT 'm';

COMMENT ON COLUMN se_measures.min_value IS
    'Interval measure lower bound (unit column''s unit) — the declarative '
    'alternative to a point value; both may coexist (a chosen point '
    'inside a declared acceptable set). se-kind.md slice 4.';
COMMENT ON COLUMN se_measures.origin IS
    'user | proposed — who owns this number. se_propose revises its own '
    '(proposed) freely and treats user rows as contract.';
COMMENT ON COLUMN se_measures.unit IS
    'Closed registry: m | count | ratio | deg. Relations require unit '
    'agreement between their endpoints; the relation scale factor is '
    'dimensionless.';

ALTER TABLE se_blocks ADD COLUMN IF NOT EXISTS origins jsonb;

COMMENT ON COLUMN se_blocks.origins IS
    'user|proposed stamps for the block''s authored facets, keyed by '
    'facet name (''envelope'', ''pose''). Absent key (or NULL) = user; '
    'the default is not stored. se-kind.md slice 4.';

COMMIT;

-- End of 0005_se_notes_freedom.sql
