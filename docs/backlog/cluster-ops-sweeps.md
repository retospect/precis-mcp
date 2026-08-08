# Cluster ops sweeps (small, batched)

Small mechanical ops chores, batched to run in one pass.

- daily_briefing role still runs `psql -d cluster` (dead DB) — repoint at
  precis_prod or remove.
- extract_watch uv-cache perm error on balthazar: a root-owned .git under
  ~deploy/.cache/uv blocks uv pip install — chown/clear.
- Orphan sweep from the feynman/quest retirement: /opt/mcps/{quest,extract},
  @companion-ai/feynman, quest's `papers` schema, unused group_vars.
- Cull orphaned tex refs from the nanotrans_auto spin (duplicate \section
  refs with workspace=∅) — one-off cleanup query.
Owner `deploy/` + prod SQL. Mechanical.
