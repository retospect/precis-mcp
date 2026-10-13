-- 0205_bom_consolidation_roles.sql
--
-- The BOM consolidation nudge (docs/backlog/bom-consolidation-nudge.md):
-- "x and y are similar, can they be combined?" asked on both bom views,
-- decided by the human. To ask it the system must know what a part IS —
-- which attributes make two parts the same part (identity), which let one
-- part stand in for another (envelope), which the design may simply change
-- (free), and which imply a tool or process rather than a part (tool).
--
-- Those roles are DATA, not code — a new part class declares them by
-- writing registry rows, exactly the ruling's "roles live in the database,
-- not in component_series.json":
--
--   * `component_specs.spec_roles` — per-spec role declaration, valid for
--     every category the spec applies to (the spec's own `category_id`
--     scopes it; universal specs keep their roles universal). Shape:
--     `{"identity": true, "free": true, "envelope": {"rule": "at_least"},
--       "tool": {"kind": "driver", "ref": "component:hex-3mm-driver"}}` —
--     any subset of the four keys; `envelope.rule` is one of
--     `at_least` (a higher rating substitutes), `at_most` (a tighter
--     tolerance substitutes), `within` (a neighbour inside `margin` of the
--     spec's canonical unit). A `tool` role names the tool KIND the
--     attribute's value implies (driver, tap, die, reflow, crimp) plus an
--     optional `ref` — the memory graph's `kind:id` typed-ref convention —
--     pointing at the tool as a ref when one exists.
--   * `component_categories.spec_roles` — the same declaration for part
--     classes that have no `component_specs` rows: the jlcparts-parametric
--     classes the pcb bom reads through `parts.params`. Shape:
--     `{"jlcparts": {"match": "<substring of params._category>",
--                    "identity": ["package"],
--                    "params": {"Resistance": {"free": true}, ...},
--                    "tool": {"package": {"kind": "reflow"}}}}`.
--     `identity` names the pseudo-params that must match (`package` =
--     `parts.package`/`footprint`, `pin_count`, `lcsc`); `params` maps
--     jlcparts attribute names to role declarations in the
--     `component_specs` shape; `tool` maps a param (or pseudo-param) name
--     to the tool it implies.
--
-- Scoped spec seeds below (`resistance`, `capacitance`) reference ONLY
-- categories this same migration creates — the 0163 lesson: a scoped
-- INSERT may not depend on another migration's seed rows, because a
-- baseline-built install carries the ledger but not the seed data.
--
-- `kept-distinct` is the human's one-word answer that suppresses a nudge:
-- "these two parts stay separate on purpose"; `links.meta.reason` carries
-- the why. SYMMETRIC, no inverse — read via `links_for(direction='both')`.
-- Keep in sync with the `Relation` Literal in `store/types.py`.
--
-- Forward-only (ADR 0005). Idempotent (ON CONFLICT DO NOTHING / IF NOT
-- EXISTS / unconditional UPDATE of the column this migration adds).

BEGIN;

-- 1. the relation ------------------------------------------------------
INSERT INTO relations (slug, is_symmetric, inverse_slug, description) VALUES
    ('kept-distinct', TRUE, NULL,
     'Both refs name parts that look mergeable but were deliberately kept '
     'separate; links.meta.reason carries the human''s why. Suppresses the '
     'BOM consolidation nudge for the pair.')
ON CONFLICT (slug) DO NOTHING;

-- 2. the role columns ----------------------------------------------------
ALTER TABLE component_categories
    ADD COLUMN IF NOT EXISTS spec_roles jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE component_specs
    ADD COLUMN IF NOT EXISTS spec_roles jsonb NOT NULL DEFAULT '{}'::jsonb;

COMMENT ON COLUMN component_categories.spec_roles IS
    'Attribute-role declaration for classes with no component_specs rows '
    '(the jlcparts-parametric classes the pcb bom reads): {"jlcparts": '
    '{"match": <params._category substring>, "identity": [...], "params": '
    '{attr: role-decl}, "tool": {attr: {"kind": ...}}}}. See 0205.';
COMMENT ON COLUMN component_specs.spec_roles IS
    'Attribute roles for the BOM consolidation nudge: which of '
    'identity/envelope/free/tool this spec plays for every category it '
    'applies to. See migration 0205 for the shape.';

-- 3. the two jlcparts-facing classes ---------------------------------------
-- Each is ALSO a valid component category (a hand-entered resistor is a
-- component with category='resistor'); the jlcparts block is what makes it
-- a pcb part class.
INSERT INTO component_categories (category_id, name, status, description, spec_roles)
VALUES
    ('resistor', 'Resistor', 'core',
     'Discrete resistors (through-hole and SMD).',
     '{"jlcparts": {"match": "Resistor", "identity": ["package"], '
     ' "params": {"Resistance": {"free": true}, '
     '            "Power(Watts)": {"envelope": {"rule": "at_least"}}, '
     '            "Tolerance": {"envelope": {"rule": "at_most"}}}, '
     ' "tool": {"package": {"kind": "reflow"}}}}'::jsonb),
    ('capacitor', 'Capacitor', 'core',
     'Discrete capacitors (through-hole and SMD).',
     '{"jlcparts": {"match": "Capacitor", "identity": ["package"], '
     ' "params": {"Capacitance": {"free": true}, '
     '            "Rated Voltage": {"envelope": {"rule": "at_least"}}, '
     '            "Tolerance": {"envelope": {"rule": "at_most"}}}, '
     ' "tool": {"package": {"kind": "reflow"}}}}'::jsonb)
ON CONFLICT (category_id) DO NOTHING;

-- 4. spec seeds for the electronic classes ----------------------------------
-- Universal (category_id NULL) where more than one class reads them —
-- same accepted trade-off as 0152/0163; a pipe carrying `package` is
-- nonsense nothing reads.
INSERT INTO component_specs
    (spec_id, name, canonical_unit, dimension, value_type, status,
     category_id, description, spec_roles)
VALUES
    ('resistance', 'Resistance', 'ohm', 'resistance', 'quantity', 'core',
     'resistor', 'Nominal resistance of the part.',
     '{"free": true}'::jsonb),
    ('capacitance', 'Capacitance', 'F', 'capacitance', 'quantity', 'core',
     'capacitor', 'Nominal capacitance of the part.',
     '{"free": true}'::jsonb),
    ('power_rating', 'Power rating', 'W', 'power', 'quantity', 'core',
     NULL, 'Maximum continuous dissipation the part is rated for.',
     '{"envelope": {"rule": "at_least"}}'::jsonb),
    ('voltage_rating', 'Voltage rating', 'V', 'voltage', 'quantity', 'core',
     NULL, 'Maximum working voltage the part is rated for.',
     '{"envelope": {"rule": "at_least"}}'::jsonb),
    ('tolerance', 'Tolerance', '%', 'ratio', 'quantity', 'core',
     NULL, 'Worst-case deviation from the nominal value, percent.',
     '{"envelope": {"rule": "at_most"}}'::jsonb),
    ('package', 'Package', NULL, 'categorical', 'text', 'core',
     NULL, 'Physical package / footprint designation (0603, SOT-23, ...).',
     '{"identity": true, "tool": {"kind": "reflow"}}'::jsonb),
    ('pin_count', 'Pin count', NULL, 'count', 'quantity', 'core',
     NULL, 'Number of electrical terminals on the part.',
     '{"identity": true}'::jsonb)
ON CONFLICT (spec_id) DO NOTHING;

-- 5. roles on the existing fastener/geometry specs ---------------------------
-- UPDATE, not a reseed of the row: spec_roles is this migration's own
-- column, so the replay order problem cannot apply.
UPDATE component_specs SET spec_roles = '{
    "identity": true, "tool": {"kind": "tap"}}'::jsonb
    WHERE spec_id = 'thread_size';
UPDATE component_specs SET spec_roles = '{"identity": true}'::jsonb
    WHERE spec_id IN ('thread_pitch', 'grade', 'point_type', 'drive_size');
UPDATE component_specs SET spec_roles = '{
    "free": true, "envelope": {"rule": "at_least"}}'::jsonb
    WHERE spec_id = 'length';
UPDATE component_specs SET spec_roles = '{"free": true}'::jsonb
    WHERE spec_id = 'head_form';
UPDATE component_specs SET spec_roles = '{
    "tool": {"kind": "driver"}}'::jsonb
    WHERE spec_id = 'drive_type';
UPDATE component_specs SET spec_roles = '{
    "tool": {"kind": "spanner"}}'::jsonb
    WHERE spec_id = 'across_flats';

COMMIT;

-- End of 0205_bom_consolidation_roles.sql
