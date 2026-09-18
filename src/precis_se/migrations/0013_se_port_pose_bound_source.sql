-- precis_se/0013_se_port_pose_bound_source.sql
--
-- `se_ports.pose_source` now has both its writers. Migration 0011 shipped
-- the declared half and stamped the column comment "'bound' = filled from
-- a realized structure the block binds (no writer yet)"; `bind_structure`
-- is that writer as of docs/backlog/port-pose-and-composition-search.md
-- §Decision 1, so the comment is restated — schema comments are what
-- `docs/reference/schema.md` and an agent reading the catalog see, and a
-- "no writer yet" that has a writer is a lie in the most-read place.
--
-- Comment only: the CHECK constraints from 0011 already admit 'bound' and
-- the shape invariants are unchanged. No data migration — every existing
-- row is 'declared' or NULL, both still correct.

COMMENT ON COLUMN se_ports.pose_source IS
    'Provenance of pose_xyz, a closed enum mirroring precis.blocktree.'
    'types.PORT_POSE_SOURCES: ''declared'' = design intent set by an '
    'agent (add_port/set_port_pose), ''bound'' = measured off the '
    'realization by se''s bind_structure (the block-local position of '
    'the atom the port resolves to). A bind fills an empty slot or '
    'refreshes its own earlier measurement; it never overwrites a '
    '''declared'' target — that disagreement is the port_pose_mismatch '
    'finding instead. NULL exactly when pose_xyz is NULL.';
