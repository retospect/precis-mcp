# Backlog

One file per open work item; delete-on-ship (`docs/README.md`). Front-matter
`status:` tracks readiness (`idea` → `draft` → `ready`, plus `in-progress`
once someone is actually building it, and `canonical` for a decided design
rule that stays here as the spec of record while its implementation slices
are filed separately — Reto's call, per item; and `parked` for a
deliberately shelved item that stays out of the pick order until
reopened); optional `prio:` (`high` | `normal` |
`low`, default `normal`) sorts the index and the autonomous fixer's pick
order high-first. `ready` means **unclaimed**: the laptop fixer builds any `ready`
item, so whoever starts building a spec (fleet builder, Devin, a session)
flips it to `in-progress` first, in its own commit to main (Reto 2026-10-23,
after the fixer built dev-loop-scratch a second time).

`draft` → `ready` is a gate, not a vibe — all of these hold:

- `## Acceptance criteria` is present, concrete and verifiable — it becomes
  the fixer's "done means X" and the post-deploy blast-radius check.
- `## Explicitly NOT in scope` names the boundary; a `ready` item with no
  stated boundary invites overreach.
- No blocker-severity open question remains in `## Open questions /
  decisions log` — resolved questions read as decisions.
- Front matter is valid (`status:`/`pillar:` pass `scripts/backlog-lint`).
- No `INDEX.md` edit — the index entry is generated from front matter.

**`status:` and `pillar:` are mandatory.** A file without front matter is
not untyped — it is rendered as `idea` by `scripts/docs-index`, i.e. it
silently claims a readiness nobody chose. `pillar:` is one of
`memory-graph` · `3d-design` · `local-compute` · `personal` (the four
pillars in `docs/roadmap.md`) or `quests` · `platform` (the two buckets
that serve them); the index groups by it. `scripts/backlog-lint` flags a
missing or out-of-set value for either key, and warns when more than eight
files share a leading token (one subject filed N times instead of one item
with N sections) — reviewed families are baselined in
[`lint-baseline.txt`](./lint-baseline.txt) and re-warn only when they grow
past the recorded count.

**Pillars:** [`docs/roadmap.md`](../roadmap.md) — what each pillar is for,
which threads are active, and the retirement rules.

**Threads:** [`threads/`](./threads/README.md) — the ordering layer. The
INDEX enumerates work; a thread file *sequences* one thread of it, as a
short ranked list of pointers with a one-line rank rationale. Pointers
only, never content. Read the thread file before picking up an item in a
thread that has one.

**Index:** [`INDEX.md`](./INDEX.md) — one line per item with status.
Generated locally and gitignored: rows come from each file's front matter,
so a spec never adds an INDEX entry by hand — it sets `status:`/`pillar:`
and the generator does the rest. The file is absent in a fresh worktree or
a session outside the SessionStart hook; if the link target is missing or
stale, run `scripts/docs-index` (stdlib-only, regenerated automatically at
session start).
