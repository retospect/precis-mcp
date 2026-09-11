-- precis_nm/0006_nm_port_annotations.sql
--
-- gripe 334769: a port had no way to declare itself intentionally left
-- unconnected (an antenna/handle port, a future attachment point not
-- wired yet), so the only way to silence ``validate``'s ``unconnected_port``
-- warn was to author a fake ``connect`` -- the exact perverse incentive the
-- dogfood exposed. ``nm_ports`` has never carried the shared blocktree
-- core's open ``annotations`` dict (0001's schema predates
-- ``precis.blocktree.types.Port.annotations`` entirely) -- add it now, the
-- same nullable jsonb shape ``nm_blocks.dof``/``nm_connects.objectives``
-- already use, so ``add_port``'s ``annotations={...}`` round-trips through
-- ``persist.py`` instead of being silently dropped on save.
--
-- Forward-only (ADR 0005). Idempotent. Plugin migration (namespace
-- `precis_nm`), applied after 0001-0005 (Migrator.discover_sources orders
-- by filename within a source). Those are sealed; never edit them, ship
-- forward instead.

BEGIN;

ALTER TABLE nm_ports ADD COLUMN IF NOT EXISTS annotations jsonb;

COMMENT ON COLUMN nm_ports.annotations IS
    'The open descriptive dict (precis.blocktree.types.Port.annotations) '
    '-- add_port(annotations={...}). NULL/absent = no annotations. '
    'validate.py gives {"external": true} a checked consumer: '
    'unconnected_port skips a port carrying it (info line instead of warn) '
    '-- gripe 334769.';

COMMIT;

-- End of 0006_nm_port_annotations.sql
