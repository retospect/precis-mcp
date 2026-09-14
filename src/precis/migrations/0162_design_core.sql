-- 0162_design_core.sql
--
-- The shared design core (docs/backlog/design-state-core.md items 1-6;
-- multiscale-design-system-spec.md §1.3-1.6, §5.6). Four subsystems that
-- recur identically at macro scale and at atomic scale, built ONCE here in
-- core rather than once per renter:
--
--   1. scenario / service environment — the production context that decides
--      which physics runs at all (`design_service_environments`,
--      `design_scenarios`, the load-case library + its per-design
--      exemptions, and the per-design scenario choice);
--   2. stable block identity — `design_block_uid_seq`, the bigint mint
--      behind the carried-forward `uid` the renter's persist layer adopts;
--   3. design history — `design_envelope_revisions`, `design_checkpoints`,
--      `design_branches`;
--   4. discrete states — `design_states`, `design_transitions` and the
--      PER-BLOCK current-state pointer `design_block_state`.
--
-- Plus `design_situations`, a deliberate STUB: ids exist so other tables can
-- reference a situation, but the three-verdict rule table is build-order
-- step 3 (`situation-rule-tables.md`) and is NOT built here.
--
-- NOT A KIND. Nothing here seeds a `kinds` row and no verb reaches it
-- directly — `se` (macro and atomic mode alike) rents this the way it
-- already rents the cad kernel, and every capability surfaces through its
-- existing ops and views. No column names a renter: these are design-level
-- concepts, so a second renter costs an import, not a migration.
--
-- WHY `block_uid` CARRIES NO FOREIGN KEY. A block lives in a PLUGIN table
-- (`se_blocks`) in a different migration namespace, which core may not
-- reference; and that plugin saves by retire-all/reinsert-all, so a block
-- ROW id is rebuilt on every save and could never be an FK target that
-- survives an edit. `uid` is the stable identity instead: minted once from
-- `design_block_uid_seq`, carried forward across saves as ordinary column
-- data, preserved by branch copies so branches diff block-by-block. Rows
-- here are keyed `(ref_id, block_uid)`; the `ref_id` FK is what keeps them
-- from outliving their design.
--
-- Vocabulary, ruled 2026-09-12 (docs/glossary.md): `scenario` = production
-- context; `situation` = named swept-volume bundle; `state` = a block's
-- physical discrete state; the versioning axis is `design history`, never
-- "design state".
--
-- Forward-only (ADR 0005). Idempotent.

BEGIN;

-- 1. stable block identity ------------------------------------------------
-- One global sequence, not per-design: a uid is unique across every design
-- so a branch copy, a cross-design instance and a viewer path leaf all mean
-- the same block without a qualifying scope. bigint, matching house style
-- (the uuid alternative was considered and dropped — design-state-core.md
-- "Open questions").
CREATE SEQUENCE IF NOT EXISTS design_block_uid_seq AS bigint START 1;

COMMENT ON SEQUENCE design_block_uid_seq IS
    'Mint for stable block uids. A block gets ONE uid for its whole life; '
    'the renter''s persist layer carries it across retire-all/reinsert-all saves '
    'as column data. Never reuse, never reset.';

-- 2. service environment ---------------------------------------------------
CREATE TABLE IF NOT EXISTS design_service_environments (
    env_id              text PRIMARY KEY,
    name                text NOT NULL,
    -- THE MASTER SWITCH, stored as two explicit columns rather than one
    -- number plus a threshold nobody agreed on: `lifetime_checks` says
    -- whether the lifetime physics FAMILY (corrosion, creep, fatigue) runs
    -- at all, `expected_lifetime_s` scales the terms once it does. A
    -- week-long prototype sets FALSE; fifty years subsea sets TRUE with a
    -- big number. A preset states both; nothing infers one from the other.
    lifetime_checks     boolean NOT NULL DEFAULT TRUE,
    expected_lifetime_s double precision,     -- NULL = unstated
    temp_min_k          double precision,     -- absolute scale, as material/rxn
    temp_max_k          double precision,
    pressure_pa         double precision,
    humidity_pct        double precision,
    -- Summary scalar for the common case; the full PSD (when one exists)
    -- goes in `vibration_spectrum`.
    vibration_grms      double precision,
    vibration_spectrum  jsonb,
    cycle_count         bigint,               -- distinct from calendar time
    duty_cycle          double precision,     -- 0..1 fraction of time loaded
    -- salt / solvent / oil / uv / ... — a flat list of exposure labels, so
    -- an array beats a jsonb bag (queryable with `&&`, no key invention).
    chemical_exposure   text[] NOT NULL DEFAULT '{}',
    status              text NOT NULL DEFAULT 'proposed'
        CHECK (status IN ('core', 'proposed')),
    description         text,
    created_at          timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE design_service_environments IS
    'What a design must survive (spec §1.3). core = seeded with the '
    'scenario presets; proposed = minted by a caller.';

-- 3. scenario ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS design_scenarios (
    scenario_id       text PRIMARY KEY,
    name              text NOT NULL,
    quantity          bigint,                 -- units to be made; NULL = unstated
    -- Weights over optimiser objectives. Deliberately an OPEN dict: the
    -- objective vocabulary belongs to the optimiser (build-order step 4),
    -- which does not exist yet, and closing it here would guess. The
    -- seeded presets use mass/cost/lead_time as starting points.
    objective_weights jsonb NOT NULL DEFAULT '{}'::jsonb,
    service_env_id    text REFERENCES design_service_environments (env_id),
    status            text NOT NULL DEFAULT 'proposed'
        CHECK (status IN ('core', 'proposed')),
    description       text,
    created_at        timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE design_scenarios IS
    'Top-level production context (spec §1.3): quantity, objective weights '
    'and the service environment, named once instead of hand-tuned per run. '
    'A design references exactly one, through design_scenario_link.';

CREATE INDEX IF NOT EXISTS design_scenarios_env_idx
    ON design_scenarios (service_env_id);

-- 4. load-case library ------------------------------------------------------
CREATE TABLE IF NOT EXISTS design_load_cases (
    case_id      text PRIMARY KEY,
    name         text NOT NULL,
    family       text NOT NULL
        CHECK (family IN ('static', 'shock', 'vibration', 'off_axis',
                          'thermal', 'fatigue')),
    -- TRUE = member of the STANDARD library: applied to every design by
    -- default and removable only by an explicit, reasoned exemption row.
    -- This is the "designs that only work statically get caught" rule.
    standard     boolean NOT NULL DEFAULT FALSE,
    -- Per-family parameters (g level, axis, duration, frequency band, ...);
    -- genuinely family-shaped, so no fixed column set covers it.
    spec         jsonb NOT NULL DEFAULT '{}'::jsonb,
    description  text,
    created_at   timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE design_load_cases IS
    'The load-case library (spec §1.3). standard = applied by default to '
    'every design; exempting one needs a design_load_case_exemptions row '
    'carrying a reason.';

CREATE INDEX IF NOT EXISTS design_load_cases_standard_idx
    ON design_load_cases (case_id) WHERE standard;

-- Extra (non-standard) cases a scenario pulls in. The standard library is
-- NOT enumerated here — it applies by default, so listing it would create a
-- second place to forget.
CREATE TABLE IF NOT EXISTS design_scenario_load_cases (
    scenario_id text NOT NULL
        REFERENCES design_scenarios (scenario_id) ON DELETE CASCADE,
    case_id     text NOT NULL
        REFERENCES design_load_cases (case_id) ON DELETE CASCADE,
    PRIMARY KEY (scenario_id, case_id)
);

CREATE INDEX IF NOT EXISTS design_scenario_load_cases_case_idx
    ON design_scenario_load_cases (case_id);

-- 5. per-design wiring ------------------------------------------------------
-- The scenario a design was built under. Core, not a per-plugin column: one
-- mechanism and one query for every design whatever mode it is in, and a
-- second renter inherits it for free — the whole point of this package.
CREATE TABLE IF NOT EXISTS design_scenario_link (
    ref_id      bigint PRIMARY KEY REFERENCES refs (ref_id) ON DELETE CASCADE,
    scenario_id text NOT NULL REFERENCES design_scenarios (scenario_id),
    set_by      text,
    set_at      timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE design_scenario_link IS
    'Which scenario governs a design. One row per design (PK on ref_id) — '
    'validate/DRC output records it so a verdict is never read out of '
    'context.';

CREATE INDEX IF NOT EXISTS design_scenario_link_scenario_idx
    ON design_scenario_link (scenario_id);

CREATE TABLE IF NOT EXISTS design_load_case_exemptions (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ref_id     bigint NOT NULL REFERENCES refs (ref_id) ON DELETE CASCADE,
    case_id    text NOT NULL
        REFERENCES design_load_cases (case_id) ON DELETE CASCADE,
    -- NOT NULL on purpose: "explicitly exempted" means someone said why.
    reason     text NOT NULL CHECK (btrim(reason) <> ''),
    set_by     text,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (ref_id, case_id)
);

CREATE INDEX IF NOT EXISTS design_load_case_exemptions_case_idx
    ON design_load_case_exemptions (case_id);

-- 6. design history ---------------------------------------------------------
-- Every tightening mints a revision; every scored result records the
-- revision it was scored under, so a stale result is DETECTABLE instead of
-- silently trusted (spec §1.5). The current revision is max(revision) for
-- the design, and the rows are the audit trail of what tightened when.
CREATE TABLE IF NOT EXISTS design_envelope_revisions (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ref_id     bigint NOT NULL REFERENCES refs (ref_id) ON DELETE CASCADE,
    revision   int NOT NULL CHECK (revision > 0),
    -- Which block tightened, when the tightening was block-local. No FK —
    -- see the header's block_uid note.
    block_uid  bigint,
    reason     text,
    set_by     text,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (ref_id, revision)
);

CREATE TABLE IF NOT EXISTS design_checkpoints (
    id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ref_id            bigint NOT NULL REFERENCES refs (ref_id) ON DELETE CASCADE,
    label             text NOT NULL,
    envelope_revision int NOT NULL DEFAULT 0,
    -- Headline numbers at checkpoint time (mass, worst utilisation, cost —
    -- whatever the kind computes), so `list` is readable without a restore.
    headline          jsonb NOT NULL DEFAULT '{}'::jsonb,
    -- The snapshot itself, OPAQUE to core: the plugin serialises its own
    -- tree and core stores/returns the blob verbatim. Core does not know
    -- the renter's block shape and must not learn it here.
    payload           jsonb NOT NULL,
    reason            text,
    set_by            text,
    created_at        timestamptz NOT NULL DEFAULT now(),
    UNIQUE (ref_id, label)
);

COMMENT ON TABLE design_checkpoints IS
    'checkpoint/restore (spec §1.5). payload is the owning plugin''s own '
    'serialised tree, opaque here — restore hands it straight back.';

-- A pin creates a BRANCH, never an overwrite (spec §5.6), so a pin is
-- always reversible and both candidates stay inspectable. Storage is a
-- naive full copy through the plugins' retire-all/reinsert-all persist
-- pattern with uids PRESERVED — that carried-forward uid is exactly what
-- makes the copy diffable block-by-block. CoW/structural sharing waits
-- until branch count measurably hurts.
CREATE TABLE IF NOT EXISTS design_branches (
    id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    -- The branch's OWN design (one row per branched design).
    ref_id            bigint NOT NULL UNIQUE
        REFERENCES refs (ref_id) ON DELETE CASCADE,
    parent_ref_id     bigint REFERENCES refs (ref_id) ON DELETE SET NULL,
    parent_branch_id  bigint REFERENCES design_branches (id) ON DELETE SET NULL,
    -- One line, required: a branch list nobody can read is a tree browser
    -- with extra steps (spec §5.6 deliberately has no tree browser).
    reason            text NOT NULL CHECK (btrim(reason) <> ''),
    headline          jsonb NOT NULL DEFAULT '{}'::jsonb,
    envelope_revision int NOT NULL DEFAULT 0,
    set_by            text,
    created_at        timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS design_branches_parent_ref_idx
    ON design_branches (parent_ref_id);
CREATE INDEX IF NOT EXISTS design_branches_parent_branch_idx
    ON design_branches (parent_branch_id);

-- 7. discrete states + stimulus-labelled transitions ------------------------
-- Schema shape owned by blocktree-library-build-plan.md §Slice 2 and
-- reproduced verbatim; it lives HERE because bistability is true macro AND
-- nano (compliant latches and hard stops · photoswitches and conformers),
-- so a mode-local table would be the same table twice. A block with no
-- declared states has exactly one implicit state, so nothing existing
-- changes shape.
CREATE TABLE IF NOT EXISTS design_states (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ref_id              bigint NOT NULL REFERENCES refs (ref_id) ON DELETE CASCADE,
    block_uid           bigint NOT NULL,
    name                text NOT NULL,
    -- Per-state envelope override (the cad mini-DSL, same TEXT shape as
    -- the renter's own block envelope column). NULL = the block's own
    -- envelope, unchanged in this state.
    envelope            text,
    -- Per-port pose overrides, keyed by port name — the ports a state moves
    -- (an E/Z flip moves the far port, not the near one). Open by nature:
    -- the key set is the block's own port names.
    port_pose_overrides jsonb,
    descr               text,
    created_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (ref_id, block_uid, name)
);

COMMENT ON TABLE design_states IS
    'A block''s declared discrete states (blocktree slice 2). Per BLOCK, '
    'never per design: two independently switchable blocks sitting in '
    'different states is the photoswitch case, which a design-level pointer '
    'cannot represent.';

CREATE TABLE IF NOT EXISTS design_transitions (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ref_id      bigint NOT NULL REFERENCES refs (ref_id) ON DELETE CASCADE,
    block_uid   bigint NOT NULL,
    from_state  text NOT NULL,
    to_state    text NOT NULL,
    -- CLOSED enum, extended only by migration. `mechanical` (force/
    -- displacement-driven snap-through) is the macro adopters' member,
    -- present from the start here; the other five are blocktree slice 2's.
    driver_kind text NOT NULL
        CHECK (driver_kind IN ('light', 'reaction', 'redox', 'ph',
                               'thermal', 'mechanical')),
    -- What drives it: an `rxn` slug, a wavelength, a named actuator.
    driver_ref  text,
    -- Driver parameters — wavelength/quantum yield for light, barrier
    -- height for thermal, snap force for mechanical. Per-driver-kind shaped.
    params      jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at  timestamptz NOT NULL DEFAULT now(),
    -- DIRECTED edges: forward and reverse are separate rows because a
    -- ratchet is exactly the case where their barriers differ (addendum A9).
    -- Storing one barrier per unordered pair would make ratchets
    -- inexpressible.
    UNIQUE (ref_id, block_uid, from_state, to_state, driver_kind),
    FOREIGN KEY (ref_id, block_uid, from_state)
        REFERENCES design_states (ref_id, block_uid, name) ON DELETE CASCADE,
    FOREIGN KEY (ref_id, block_uid, to_state)
        REFERENCES design_states (ref_id, block_uid, name) ON DELETE CASCADE,
    CHECK (from_state <> to_state)
);

-- The from-side FK rides the UNIQUE index's leading prefix; the to-side
-- needs its own.
CREATE INDEX IF NOT EXISTS design_transitions_to_idx
    ON design_transitions (ref_id, block_uid, to_state);

-- PER-BLOCK current state. There is deliberately NO design-level pointer.
-- Keyed `(ref_id, block_uid)`, NOT `block_uid` alone: a uid is PRESERVED
-- across branch copies (same uid, different ref_id — the header note and
-- design_branches above), so a bare-uid PK would let a branch's own
-- ON CONFLICT upsert steal and overwrite its parent's (or a sibling
-- branch's) current-state row for that block. See set_current_state.
CREATE TABLE IF NOT EXISTS design_block_state (
    ref_id     bigint NOT NULL REFERENCES refs (ref_id) ON DELETE CASCADE,
    block_uid  bigint NOT NULL,
    state_name text NOT NULL,
    set_by     text,
    set_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (ref_id, block_uid),
    FOREIGN KEY (ref_id, block_uid, state_name)
        REFERENCES design_states (ref_id, block_uid, name) ON DELETE CASCADE
);

COMMENT ON TABLE design_block_state IS
    'Which state each block is currently in, per (ref_id, block_uid) — a '
    'uid is preserved across branch copies, so keying on block_uid alone '
    'would let a branch steal its parent''s row. HYSTERESIS (addendum A9): '
    'a state-carrying block is the one place history is load-bearing — '
    'state is NOT a function of the parameter vector — so any cache or '
    'solver key over such a block MUST include this state. See '
    'precis.design.states.';

CREATE INDEX IF NOT EXISTS design_block_state_state_idx
    ON design_block_state (ref_id, block_uid, state_name);

-- 8. situation — STUB -------------------------------------------------------
-- Ids exist so other tables and ops can reference a situation by id. The
-- rule table (must-clear / may-touch / must-contact, the three-verdict
-- machinery) is build-order step 3 and is NOT built here; adding columns to
-- this table is that item's job, not a passing edit.
CREATE TABLE IF NOT EXISTS design_situations (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ref_id     bigint NOT NULL REFERENCES refs (ref_id) ON DELETE CASCADE,
    name       text NOT NULL,
    descr      text,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (ref_id, name)
);

COMMENT ON TABLE design_situations IS
    'STUB (design-state-core.md item 1): a named swept-volume bundle — '
    'assembly, maintenance, shipping. The three-verdict rule table is '
    'build-order step 3 (situation-rule-tables.md).';

-- 9. seed: service environments, scenario presets, standard load cases ------
INSERT INTO design_service_environments
    (env_id, name, lifetime_checks, expected_lifetime_s, temp_min_k, temp_max_k,
     cycle_count, duty_cycle, chemical_exposure, status, description)
VALUES
    ('bench_week', 'Bench, one week', FALSE, 604800.0, 288.0, 303.0,
     1000, 0.05, '{}', 'core',
     'A prototype that lives on a bench for a week. lifetime_checks FALSE '
     'drops corrosion, creep and fatigue entirely — they are not what kills '
     'this design.'),
    ('indoor_5yr', 'Indoor service, five years', TRUE, 157680000.0, 283.0, 313.0,
     100000, 0.2, '{}', 'core',
     'Ordinary indoor service. Fatigue and creep matter; corrosion is mild.'),
    ('general_10yr', 'General service, ten years', TRUE, 315360000.0, 243.0, 333.0,
     1000000, 0.4, '{salt,uv}', 'core',
     'Outdoor-capable general service: the lifetime families all run, and '
     'salt plus UV exposure are declared.')
ON CONFLICT (env_id) DO NOTHING;

-- The three presets differ in exactly the two things a caller would
-- otherwise hand-tune: weights and quantity (and, through the service
-- environment, whether the lifetime physics runs at all).
INSERT INTO design_scenarios
    (scenario_id, name, quantity, objective_weights, service_env_id, status,
     description)
VALUES
    ('prototype', 'Prototype', 1,
     '{"lead_time": 0.6, "cost": 0.3, "mass": 0.1}'::jsonb,
     'bench_week', 'core',
     'One-off. Getting it in hand beats getting it light or cheap; the '
     'lifetime families are off.'),
    ('small_batch', 'Small batch', 100,
     '{"cost": 0.4, "mass": 0.3, "lead_time": 0.3}'::jsonb,
     'indoor_5yr', 'core',
     'Tens to hundreds. Per-unit cost starts to bite and the parts have to '
     'last; tooling still does not pay for itself.'),
    ('mass_production', 'Mass production', 100000,
     '{"cost": 0.6, "mass": 0.3, "lead_time": 0.1}'::jsonb,
     'general_10yr', 'core',
     'Tooling amortises, so unit cost dominates and lead time barely '
     'registers. Full lifetime physics.')
ON CONFLICT (scenario_id) DO NOTHING;

-- The standard library: applied by default, exemptable only with a reason.
-- Shock / vibration / off-axis are the three the spec names — the set that
-- catches "works statically, fails in the van".
INSERT INTO design_load_cases
    (case_id, name, family, standard, spec, description)
VALUES
    ('std_shock', 'Handling shock', 'shock', TRUE,
     '{"peak_g": 25.0, "duration_s": 0.011, "pulse": "half_sine"}'::jsonb,
     'A dropped or knocked assembly. Half-sine pulse, applied along each '
     'principal axis in turn.'),
    ('std_vibration', 'Transport vibration', 'vibration', TRUE,
     '{"grms": 1.5, "band_hz": [5.0, 500.0]}'::jsonb,
     'Broadband transport vibration — the case that finds resonances a '
     'static check cannot see.'),
    ('std_off_axis', 'Off-axis loading', 'off_axis', TRUE,
     '{"fraction_of_primary": 0.25, "cone_deg": 30.0}'::jsonb,
     'A quarter of the primary load applied off the intended line of '
     'action. Catches designs that only work in one direction.')
ON CONFLICT (case_id) DO NOTHING;

COMMIT;
