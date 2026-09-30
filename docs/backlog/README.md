# Backlog

One file per open work item; delete-on-ship (`docs/README.md`). Front-matter
`status:` tracks readiness (`idea` → `draft` → `ready`); optional `prio:`
(`high` | `normal` | `low`, default `normal`) sorts the index and the
autonomous fixer's pick order high-first.

**Threads:** [`threads/`](./threads/README.md) — the ordering layer. The
INDEX enumerates work; a thread file *sequences* one thread of it, as a
short ranked list of pointers with a one-line rank rationale. Pointers
only, never content. Read the thread file before picking up an item in a
thread that has one.

**Index:** [`INDEX.md`](./INDEX.md) — one line per item with status.
Generated locally and gitignored; if the link target is missing or stale,
run `python3 scripts/docs-index` (stdlib-only, regenerated automatically at
session start).
