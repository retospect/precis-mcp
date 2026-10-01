# Backlog

One file per open work item; delete-on-ship (`docs/README.md`). Front-matter
`status:` tracks readiness (`idea` → `draft` → `ready`, plus `in-progress`
once someone is actually building it, and `canonical` for a decided design
rule that stays here as the spec of record while its implementation slices
are filed separately — Reto's call, per item); optional `prio:` (`high` | `normal` |
`low`, default `normal`) sorts the index and the autonomous fixer's pick
order high-first.

**`status:` and `pillar:` are mandatory.** A file without front matter is
not untyped — it is rendered as `idea` by `scripts/docs-index`, i.e. it
silently claims a readiness nobody chose. `pillar:` is one of
`memory-graph` · `3d-design` · `local-compute` · `personal` (the four
pillars in `docs/roadmap.md`) or `quests` · `platform` (the two buckets
that serve them); the index groups by it. `scripts/backlog-lint` flags a
missing or out-of-set value for either key, and warns when more than eight
files share a leading token (one subject filed N times instead of one item
with N sections).

**Pillars:** [`docs/roadmap.md`](../roadmap.md) — what each pillar is for,
which threads are active, and the retirement rules.

**Threads:** [`threads/`](./threads/README.md) — the ordering layer. The
INDEX enumerates work; a thread file *sequences* one thread of it, as a
short ranked list of pointers with a one-line rank rationale. Pointers
only, never content. Read the thread file before picking up an item in a
thread that has one.

**Index:** [`INDEX.md`](./INDEX.md) — one line per item with status.
Generated locally and gitignored; if the link target is missing or stale,
run `python3 scripts/docs-index` (stdlib-only, regenerated automatically at
session start).
