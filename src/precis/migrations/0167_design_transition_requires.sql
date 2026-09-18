-- 0167_design_transition_requires.sql
--
-- port-pose-and-composition-search.md Decision 3: a requirement is a box
-- with ports and interval constraints, stored on the transition it
-- describes, not folded into `params`. `params` is the realization's
-- per-driver numbers (quantum yield, barrier height, the 436 nm PSS on
-- `se:azo-unit`) — a MEASURED figure. `requires` is the DECLARED target a
-- realization is checked against (delta between ports, span, stimulus,
-- bistable, cycles, ...) — same declared/bound split the port pose slot
-- (`se_ports.pose`) made, so a reader can tell a measured number from a
-- wanted one without a naming convention inside one JSON blob.
--
-- Declared intent only: nothing in validate/drc/clearance reads this column
-- — the sole consumer is `search(kind='se', compose='<design>#<block>')`
-- (precis_se.compose.resolve_compose), which reads the box back off a
-- block's declared transition. Write path vets it at op time
-- (`declare_transitions` -> `precis_se.compose.parse_requires`), the same
-- box/wants vocabulary `compose=` reads.
--
-- Forward-only (ADR 0005); 0162 (which created `design_transitions`) is
-- sealed. Idempotent: `ADD COLUMN IF NOT EXISTS` + a default backfills every
-- existing row with `{}` (no requirement), so a row written before this
-- migration reads identically to one written with an explicit `requires={}`.
--
-- Regenerate the baseline snapshot after merge (ADR 0031): `scripts/bump` /
-- `precis db dump-schema`.

BEGIN;

ALTER TABLE design_transitions
    ADD COLUMN IF NOT EXISTS requires jsonb NOT NULL DEFAULT '{}'::jsonb;

COMMENT ON COLUMN design_transitions.requires IS
    'DECLARED target this transition is checked against (delta between '
    'ports, span, stimulus, bistable, cycles, ...) — distinct from params, '
    'the REALIZATION''s per-driver numbers (quantum yield, barrier height). '
    'Vetted at write time by precis_se.compose.parse_requires; read back by '
    'search(kind=''se'', compose=''<design>#<block>'') '
    '(port-pose-and-composition-search.md Decision 3). {} means no '
    'requirement. Declared intent only — no validate/drc/clearance pass '
    'reads it.';

COMMIT;

-- End of 0167_design_transition_requires.sql
