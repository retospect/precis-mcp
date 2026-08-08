# Tier-2 DB role-enforce held — pgbouncer transaction pool breaks SET ROLE

PRECIS_MCP_DB_ROLE_ENFORCE (session-level SET ROLE) is only correct on a
direct-to-Postgres DSN, not pgbouncer's transaction pool (the agent :6432
DSN) — a real fix needs a direct-pg route around pgbouncer, a
security-posture decision, not a mechanical flip. `GRANT agent_ro TO
agent_rw` is already applied to prod. Owner
`src/precis/store/pool.py::_apply_db_role`. Blocked on that decision.
