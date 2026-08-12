# Backlog

One file per open work item; delete-on-ship (`docs/README.md`). Front-matter
`status:` tracks readiness (`idea` → `draft` → `ready`); optional `prio:`
(`high` | `normal` | `low`, default `normal`) sorts the index and the
autonomous fixer's pick order high-first.

**Index:** [`INDEX.md`](./INDEX.md) — one line per item with status.
Generated locally and gitignored; if the link target is missing or stale,
run `python3 scripts/docs-index` (stdlib-only, regenerated automatically at
session start).
