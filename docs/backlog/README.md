# Backlog

**Specifications live in graph memory, not here** (Reto 2026-11-05,
`me568168`). A spec is a hub of small linked memory nodes under the
Specifications hub `me568215`: a spec root, its section nodes, its rulings
and gotchas, and a plan todo that carries execution state. The format, the
writer procedure and how each consumer reads it are in
`get(kind='skill', id='precis-spec-help')`. A small item — an idea, a
finding, a one-paragraph annoyance — is a **gripe**
(`put(kind='gripe', text='<command + expected + actual>')`), not a file and
not a memory node. Do not add spec files to this directory.

What remains here:

- **[`threads/`](./threads/README.md)** — the ordering layer, unchanged
  convention. A thread file *sequences* one thread of work as a short
  ranked list of pointers with a one-line rank rationale. Pointers only,
  never content. Read the thread file before picking up work in a thread
  that has one.
- **[`INDEX.md`](./INDEX.md)** — generated and gitignored; rows come from
  each remaining file's front matter, so nothing adds an INDEX entry by
  hand. Absent in a fresh worktree or a session outside the SessionStart
  hook; if the link target is missing or stale, run `scripts/docs-index`
  (stdlib-only, regenerated automatically at session start). The graph
  needs no counterpart: `search(kind='memory', tags=['spec:*'],
  view='toc')` lists every spec root wherever it hangs.
- **[`TEMPLATE.md`](./TEMPLATE.md)** — the legacy file form, kept for the
  one file below. New specs use the graph shape in `precis-spec-help`.
- **[`lint-baseline.txt`](./lint-baseline.txt)** — the crowded-prefix
  baseline `scripts/backlog-lint` reads.
- **[`se-protein-interface-first.md`](./se-protein-interface-first.md)** —
  one legacy spec file, deliberately not transferred to the graph. It keeps
  working under the file convention described below.
- **`llm-judge-reliability-data/`** — the replicate/gold data for the
  LLM-judge reliability measurement, with its scoring scripts.

## Front matter, for the remaining file and for thread files

`status:` tracks readiness (`idea` → `draft` → `ready`, plus `in-progress`
once someone is actually building it, and `parked` for a deliberately
shelved item that stays out of the pick order until reopened). `ready`
means **unclaimed**: the laptop fixer builds any `ready` item, so whoever
starts building flips it to `in-progress` first, in its own commit to main
(Reto 2026-10-23, after the fixer built dev-loop-scratch a second time).
Optional `prio:` (`high` | `normal` | `low`, default `normal`) sorts the
index and the fixer's pick order high-first.

`draft` → `ready` is a gate, not a vibe — all of these hold (the graph
equivalent is `precis-spec-help`'s build-ready slice: concrete trigger,
expected result, acceptance evidence, with missing fixtures or thresholds
named as decisions on the plan todo):

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

**`prior-art:` is the optional source field.** An item taken from a paper,
repo or memory names it as decimal handles — `prior-art: pa57193,
web:608520` — which `scripts/backlog-lint` resolves against prod (a dead or
mistyped handle fails the lint; prod unreachable is a quiet note). A
`Steal identified` / `Adapted from` sentence covered by no handle field
warns: prose-only sources rot silently (gr610793). In the graph the same
duty is a `cites`/`derived-from` edge on the section node.

**Pillars:** [`docs/roadmap.md`](../roadmap.md) — what each pillar is for,
which threads are active, and the retirement rules.
