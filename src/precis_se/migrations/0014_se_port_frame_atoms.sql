-- precis_se/0014_se_port_frame_atoms.sql
--
-- `se_ports.axis_atom` / `phase_atom` — the two extra atom labels R1's
-- object form of `bind_structure`'s `ports=` mapping supplies alongside
-- `bound_atom` (docs/backlog/port-rotation-and-lever-composition.md,
-- Slice R1: "ports carry a measured rotation"). `bound_atom` is the port
-- itself (the attachment atom); `axis_atom` is the atom that, together
-- with `bound_atom`, defines the frame's z (the axle: `bound_atom` →
-- `axis_atom`, an atom→atom bond, e.g. stator → rotor); `phase_atom`
-- fixes the roll — its projection onto the plane normal to z is the
-- frame's x. Both or neither, and `axis_atom` never equal to `bound_atom`
-- — `bind_structure` rejects either shape at op time; no CHECK here
-- mirrors it, same as `bound_design`/`bound_atom` themselves (0007) carry
-- none.
--
-- `rot_source` — `pose_rot`'s OWN provenance column, alongside migration
-- 0011's `pose_source` (which, despite its name, had only ever governed
-- `pose_xyz`/`pose_rot` as ONE fact). R1's bind-time bug fix: a port can
-- carry a `'declared'` pose and a `'bound'` (or never-yet-measured) rot,
-- or vice versa — `bind_structure` measures/compares each field by its
-- OWN source, never the other one's. Same closed enum as `pose_source`
-- (`precis.blocktree.types.PORT_POSE_SOURCES`), and — per
-- `precis.blocktree.types.Port`'s own docstring, which promises this for
-- every domain that persists ports — the SAME two CHECKs `pose_source`
-- carries, mirrored onto `rot_source`: a closed-enum check and a
-- per-field shape check (`(pose_rot IS NULL) = (rot_source IS NULL)`,
-- 0011's `se_ports_pose_shape_check` restated for the rot half instead
-- of coupling it to `pose_source`).
--
-- **Backfill, before the shape CHECK can be added**: 0011 already
-- shipped `pose_source`/`pose_rot` and has been live since — a prod row
-- written by `set_port_pose(rot=...)` (or `add_port(pose=..., rot=...)`)
-- before this migration has `pose_rot IS NOT NULL` with no `rot_source`
-- at all (the column didn't exist yet). Every such write was an agent
-- statement, not a measurement (`bind_structure` grew a rot writer only
-- in THIS slice), so backfilling `'declared'` is not a guess — it is
-- what the row's own history means. Without this UPDATE the new shape
-- CHECK would be violated by the very rows migrating it is meant to run
-- against.
--
-- All three (`axis_atom`/`phase_atom`/`rot_source`) nullable, like
-- `bound_atom` itself — the great majority of ports carry no frame at
-- all, only a position.

BEGIN;

ALTER TABLE se_ports ADD COLUMN IF NOT EXISTS axis_atom text;
ALTER TABLE se_ports ADD COLUMN IF NOT EXISTS phase_atom text;
ALTER TABLE se_ports ADD COLUMN IF NOT EXISTS rot_source text;

COMMENT ON COLUMN se_ports.axis_atom IS
    'The atom whose bond to bound_atom (the port''s own atom) is the '
    'measured frame''s z axis (the axle): atom -> axis_atom, unit '
    'vector, in the structure''s (block-local) frame. NULL unless '
    'phase_atom is also set (bind_structure''s object ports= form; '
    'docs/backlog/port-rotation-and-lever-composition.md Slice R1).';
COMMENT ON COLUMN se_ports.phase_atom IS
    'The atom whose direction from bound_atom, projected onto the '
    'plane normal to the axis_atom axis, fixes the measured frame''s x '
    '(a rotor substituent fixes the roll). NULL unless axis_atom is '
    'also set. docs/backlog/port-rotation-and-lever-composition.md '
    'Slice R1.';
COMMENT ON COLUMN se_ports.rot_source IS
    'Provenance of pose_rot, a closed enum mirroring precis.blocktree.'
    'types.PORT_POSE_SOURCES — the SAME two values pose_source uses '
    '(''declared''/''bound''), but its OWN independent stamp '
    '(se_ports_rot_source_check / se_ports_rot_shape_check): a port''s '
    'pose and its rot each carry their own provenance (R1, '
    'docs/backlog/port-rotation-and-lever-composition.md), so a '
    'declared pose with a bound (or never-measured) rot, or the '
    'reverse, is a normal state, not a conflict. NULL exactly when '
    'pose_rot is NULL.';

-- Backfill BEFORE the shape CHECK below: every pre-existing pose_rot
-- (written by 0011's pose_source='declared' path — bind_structure's rot
-- writer is new in this slice, so no prior row can be 'bound') gets the
-- provenance its own history already implies.
UPDATE se_ports
    SET rot_source = 'declared'
    WHERE pose_rot IS NOT NULL AND rot_source IS NULL;

-- Mirrors 0011's se_ports_pose_source_check, for rot_source instead of
-- pose_source. NOT VALID (the 0007 precedent — the backfill above makes
-- every existing row already satisfy this, and a NOT VALID constraint is
-- enforced on every future write without the full-table scan a
-- validating ADD CONSTRAINT would take on a deployed corpus).
ALTER TABLE se_ports DROP CONSTRAINT IF EXISTS se_ports_rot_source_check;
ALTER TABLE se_ports ADD CONSTRAINT se_ports_rot_source_check
    CHECK (rot_source IS NULL OR rot_source IN ('declared', 'bound'))
    NOT VALID;

-- Mirrors 0011's se_ports_pose_shape_check's rot clause, but keyed on
-- rot_source instead of pose_source — the invariant the ops enforce,
-- restated where the data actually lives: rot_source is set exactly when
-- pose_rot is, independent of what pose_source says.
ALTER TABLE se_ports DROP CONSTRAINT IF EXISTS se_ports_rot_shape_check;
ALTER TABLE se_ports ADD CONSTRAINT se_ports_rot_shape_check
    CHECK ((pose_rot IS NULL) = (rot_source IS NULL))
    NOT VALID;

COMMIT;

-- End of 0014_se_port_frame_atoms.sql
