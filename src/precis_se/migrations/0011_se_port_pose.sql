-- precis_se/0011_se_port_pose.sql
--
-- The port pose slot (gr342026; docs/backlog/
-- port-pose-and-composition-search.md §Decision 1): a port gets a
-- placement OF ITS OWN in its block's local frame, mirroring the block's
-- `pose_xyz`/`pose_rot` one level down — same column names, same units
-- (metres / SI radians, Euler `Rz@Ry@Rx`), same frame convention as
-- `direction` and `envelope` already use (`precis_se.atomic.validate.
-- envelope_fit`).
--
-- Why this is a *nullable* slot rather than a required one: at box level
-- the exact displacement from a block's origin to its attachment point is
-- often genuinely unknown, and a fabricated number is indistinguishable
-- downstream from a measured one. So every geometry consumer keeps its
-- existing envelope-extent approximation for the null case and says in
-- its output which of the two it used — `bond_length_sanity` is the
-- worked example (`precis_se.atomic.validate._bond_length_findings`).
--
-- Why provenance is stored, not inferred: `pose_source='declared'` is
-- design INTENT (an agent said where the port sits — a target a
-- realization is checked against), `'bound'` is MEASUREMENT (the
-- displacement read back off a realized structure the block binds).
-- Those are different claims and the consumers will treat them
-- differently. `'bound'` has no writer yet; the enum value and its CHECK
-- exist now so the writer needs no second migration
-- (`precis.blocktree.types.PORT_POSE_SOURCES` is the same closed enum in
-- Python, and this CHECK is its mirror).
--
-- Both CHECKs land NOT VALID (the 0007 precedent): every existing row has
-- all three columns NULL and so already satisfies them, and a NOT VALID
-- constraint is enforced on every future write without the full-table
-- scan a validating ADD CONSTRAINT would take on a deployed corpus.

BEGIN;

ALTER TABLE se_ports ADD COLUMN IF NOT EXISTS pose_xyz double precision[];
ALTER TABLE se_ports ADD COLUMN IF NOT EXISTS pose_rot double precision[];
ALTER TABLE se_ports ADD COLUMN IF NOT EXISTS pose_source text;

COMMENT ON COLUMN se_ports.pose_xyz IS
    'The port''s own origin in its block''s LOCAL frame, METRES [x,y,z] '
    '(the block''s se_blocks.pose_xyz one level down). NULL = this port '
    'has no stored position of its own; consumers fall back to the '
    'owning block''s pose plus an envelope-extent approximation and say '
    'so. gr342026 / docs/backlog/port-pose-and-composition-search.md.';
COMMENT ON COLUMN se_ports.pose_rot IS
    'The port frame''s orientation, SI RADIANS [rx,ry,rz], Euler '
    'Rz@Ry@Rx — same convention as se_blocks.pose_rot (migration 0006). '
    'Requires pose_xyz: a rotation with no origin is meaningless '
    '(se_ports_pose_shape_check). NULL with a pose_xyz set = unrotated.';
COMMENT ON COLUMN se_ports.pose_source IS
    'Provenance of pose_xyz, a closed enum mirroring precis.blocktree.'
    'types.PORT_POSE_SOURCES: ''declared'' = design intent set by an '
    'agent (add_port/set_port_pose), ''bound'' = filled from a realized '
    'structure the block binds (no writer yet — the value exists so the '
    'later one needs no migration). NULL exactly when pose_xyz is NULL.';

ALTER TABLE se_ports DROP CONSTRAINT IF EXISTS se_ports_pose_source_check;
ALTER TABLE se_ports ADD CONSTRAINT se_ports_pose_source_check
    CHECK (pose_source IS NULL OR pose_source IN ('declared', 'bound'))
    NOT VALID;

-- The two invariants the ops enforce, restated where the data actually
-- lives: provenance exists exactly when a pose does, and a rotation never
-- floats free of an origin.
ALTER TABLE se_ports DROP CONSTRAINT IF EXISTS se_ports_pose_shape_check;
ALTER TABLE se_ports ADD CONSTRAINT se_ports_pose_shape_check
    CHECK (
        (pose_xyz IS NULL) = (pose_source IS NULL)
        AND (pose_rot IS NULL OR pose_xyz IS NOT NULL)
    )
    NOT VALID;

COMMIT;

-- End of 0011_se_port_pose.sql
