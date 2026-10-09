-- 0191_pcb_drc_finding_keys.sql
--
-- docs/backlog/finding-stable-identity.md — a DRC finding gets an identity
-- that survives the next run, so two runs of the same check can be diffed
-- ("new / still / gone") instead of compared by eye (Reto, 2026-09-24:
-- "Findings need stable identity ... allows to see if 'this error' or
-- 'both these' have gone away").
--
-- Four columns on ``pcb_drc_findings`` (0138), all nullable so the rows
-- already there stay valid — a pre-0191 row has no key and is simply not
-- diffable (``Store.pcb_drc_findings_latest`` reports a keyless previous
-- run as "nothing to compare against", never as "all new"):
--
--   * ``finding_key``   — :func:`precis.pcb.drc.finding_key`: a content
--     hash over ``(rule, canonicalised participants, layer)``, never over
--     coordinates, the measured margin, list position or run id. The two
--     nets of a clearance hit, the pad and the via of a keep-out — the
--     durable things the finding is ABOUT. Recomputable from the row's
--     own ``objects``; no sequence, no counter.
--   * ``margin_mm``     — the signed shortfall, persisted so "still, but
--     worse (-0.114 -> -0.090)" is reportable. Payload, not identity.
--   * ``first_seen_at`` — the ``created_at`` of the earliest row on this
--     board with the same key, copied forward on write; a finding has a
--     lifetime, not only a present. Last-seen is the latest row itself.
--   * ``location``      — the finding's ``where`` label (``R3/2 <-> C1/1
--     on F.Cu``), so a finding that is GONE in the current run can still
--     be named from its previous row; ``where`` is an SQL keyword, hence
--     the name.
--
-- And ``pcb_drc_runs``: one row per run, written whether or not the run
-- produced findings. Before this, a CLEAN run left no trace at all — zero
-- finding rows — so "the previous run" could only ever resolve to the
-- last run that had findings, and a finding that went away and came back
-- would read as "still" rather than "new again". It also means
-- ``pcb_drc_findings_latest`` can tell a clean run (a run_id, no rows)
-- from no run yet (``None``) — the ``netlist_drc_clean`` gate's own
-- "not yet" vs "clean" distinction, which the finding rows alone could
-- not carry.
--
-- No backfill of ``finding_key`` for pre-0191 rows: the key is a Python
-- derivation over ``objects`` with per-rule participant extraction and
-- would have to be re-stated in SQL to backfill here. Legacy runs stay
-- readable and undiffable; the first post-0191 run starts the lineage.

BEGIN;

ALTER TABLE pcb_drc_findings
    ADD COLUMN IF NOT EXISTS finding_key   text,
    ADD COLUMN IF NOT EXISTS margin_mm     double precision,
    ADD COLUMN IF NOT EXISTS first_seen_at timestamptz,
    ADD COLUMN IF NOT EXISTS location      text;

COMMENT ON COLUMN pcb_drc_findings.finding_key IS
    'Stable content-derived identity (precis.pcb.drc.finding_key) over '
    '(rule, canonicalised participants, layer) — never coordinates, margin '
    'or position. NULL on rows written before 0191.';
COMMENT ON COLUMN pcb_drc_findings.margin_mm IS
    'Signed shortfall below the tier threshold (negative = violated); '
    'payload, not identity — a changed margin is the same finding.';
COMMENT ON COLUMN pcb_drc_findings.first_seen_at IS
    'created_at of the earliest row on this board carrying the same '
    'finding_key — the finding''s lifetime start; last-seen is this row.';
COMMENT ON COLUMN pcb_drc_findings.location IS
    'The finding''s human ``where`` label; lets a gone finding be named '
    'from its previous row. NULL on rows written before 0191.';

CREATE INDEX IF NOT EXISTS pcb_drc_findings_board_key_idx
    ON pcb_drc_findings (board_id, finding_key)
    WHERE finding_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS pcb_drc_runs (
    board_id   bigint NOT NULL REFERENCES pcb_boards (board_id) ON DELETE CASCADE,
    run_id     text   NOT NULL,
    pads_only  boolean NOT NULL DEFAULT false,
    n_error    integer NOT NULL DEFAULT 0,
    n_warn     integer NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (board_id, run_id)
);

COMMENT ON TABLE pcb_drc_runs IS
    'One row per DRC run (view=''drc'' / view=''gerber''), written even when '
    'the run is clean — the previous-run anchor for new/still/gone deltas, '
    'and what lets a clean run be told apart from no run at all.';

CREATE INDEX IF NOT EXISTS pcb_drc_runs_board_created_idx
    ON pcb_drc_runs (board_id, created_at DESC);

COMMIT;

-- End of 0191_pcb_drc_finding_keys.sql
