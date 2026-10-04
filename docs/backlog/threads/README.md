# Thread files — the ordering layer over `docs/backlog/`

`docs/backlog/` holds 400+ items. It answers "what is open?" and cannot
answer "what do I do next, and why that?" — an index sorted by status and
priority flag does not know that one item silently corrupts data while
another is a convenience, or that three items share a root cause.

A **thread file** answers that, for one thread of work. It is a short,
ordered list of **pointers with a rank rationale**. It owns order and
nothing else. `INDEX.md` in this directory groups the threads into
programmes and records the seams where two threads touch the same code or
wait on each other — coordination reads one page, ranking stays per file.

## The one rule

**Pointers only. Never content.**

If you are explaining what an item *is*, you are writing in the wrong
file — that belongs in the item (`docs/backlog/<slug>.md`) or the gripe.
A thread entry says where the thing is and why it sits at that rank, in
one line. This is the rule that keeps the ordering layer from becoming a
second backlog, which is the failure mode it exists to prevent.

## Resume entry point

Start with [PRIORITIES.md](PRIORITIES.md); run `python3 scripts/docs-index`
if missing or stale. Declared activity lives only in
`.claude/fleet/threads.tsv`; verify live work with `scripts/inflight`.

Each thread starts with one `## Resume` section, at most 350 words, with
one bullet for each field: `Pillar`, `Next`, `Blocked by`, `Unblocks`,
`Acceptance`, `Worktree`, `Builds`, `Detail`. Use a closed pillar value
from the backlog contract. Use clickable relative links with stable item
names for dependencies. Keep estimates in builds; explicitly say when
unknown. Check current deployment before repeating a handoff's operation.
Update Resume at the four thread moments below, then regenerate the table.
Read the current item and immediate dependencies next; detailed handoffs
are loaded only when needed. Existing detailed records remain during this
migration; move durable instructions to runbooks and preserve decisions
in their owning items/docstrings before removing shipped history.

## Shape

One file per thread: `docs/backlog/threads/<slug>.md`.

```markdown
# <thread name>

**Status:** ends when <the end state, one sentence; point at the spec or
north-star item that holds it>. Today <where it stands, and the ordering
rule for the list below>.
**Last reviewed:** YYYY-MM-DD
**Worktree:** `<slug>`

## Do next

1. **<pointer>** — <why this rank: what it unblocks, or what it prevents>
2. **<pointer>** — <…>

## Horizon

1. **<pointer>** — <what it delivers, and what it waits on>

## Parked

- **<pointer>** — <the condition that unparks it>

## No action needed

- **<pointer>** — <ruled / not a bug / already fixed, pending close>
```

A pointer is one of: `backlog/<slug>.md` · `gr<id>` · `td<id>` ·
`qu<id>` · a named deliverable not yet filed (file it before it can be
ranked above the parked section).

**Status** is the thread's mission in two sentences. The first names the
end state — what "this thread is done" looks like — and points at the
document that holds it where one exists. A thread whose end state cannot
be tied to `docs/mission.md` in a clause is a chore list, not a thread.
The second says where the thread stands and the rule that orders the list.
Wherever a thread file says how far out something is — Status, a why-line,
a Resume block, an eta for the round — the unit is builds or dev cycles
("slice 2: one build; hero: ~3 cycles"), never a calendar date. Reto,
2026-10-03: date estimates are time wasted; the number of builds out is
the estimate he reads.

**Horizon** is the longer-range plan: the ordered milestones that come
after `## Do next` empties — spec sections, multi-slice items, ADRs, named
deliverables. Same rule: pointers with a why-line, never content. Unlike
`## Parked`, a horizon entry is not blocked on an external condition; it
is sequenced later by choice. A thread whose horizon is genuinely empty
writes `(none)` so the absence is a statement, not an omission.

**Worktree** names the tree this thread's work happens in; by convention
it is the thread slug. "Resume the work on <thread>" means: read this
file; check `scripts/inflight` for a live or dirty tree whose purpose
names this thread and enter that one (uncommitted work lives there);
otherwise `claude -w <slug>` (or EnterWorktree with that name); write the
Status line's second sentence into `.claude/purpose`; start at Do-next 1.
A tree named for a thread is reaped like any other once merged, clean and
sessionless; the name, not the tree, is the durable handle.

## Ranking

In order of precedence:

1. **Dependency.** If B cannot be done, or cannot be verified, until A
   lands, A goes first. Say so in the why-line.
2. **Blast radius.** Silent data corruption beats a broken feature beats
   an inert feature beats a cosmetic one. A defect that writes wrong data
   and emits no finding outranks a defect that fails loudly.
3. **Leverage.** Tooling that makes the rest of the list cheaper to
   diagnose earns a rank above the items it serves, but never above a
   live corruption.
4. **Cost.** Tie-break only. Cheap does not buy rank.

Every entry's why-line must name what it unblocks or what it prevents. If
you cannot write that line, the item is not understood well enough to
rank — leave it parked.

## When to update

Four moments, and not otherwise:

- **Filing a gripe or backlog item** in this thread — insert it at its
  rank now, while you know why.
- **Landing a change** in this thread — delete what shipped, re-rank what
  its landing changed.
- **`/next`** — the handoff reads the thread file, so a stale list
  produces a stale handoff.
- **Finding out a rank was wrong** — including from a dogfood. Re-rank
  and say in the why-line what changed your mind.

A change that adds or removes a seam between threads updates `INDEX.md`
in the same commit.

## Lifecycle

- **Create** a thread file when a thread has three or more live items.
  Below that the items carry themselves.
- **Delete** entries on ship, in the same commit as the ship. The file
  shrinks as the thread closes.
- **Retire** the file when `## Do next` empties and `## Horizon` is
  `(none)`.
- **Split** the thread if `## Do next` plus `## Parked` passes roughly 40
  lines. A list too long to hold in the head is a list nobody re-ranks.
  The horizon does not count against the split: it is read, not re-ranked
  weekly.

Thread files live in a subdirectory, so `scripts/docs-index` (which globs
`docs/backlog/*.md`) does not fold them into the item INDEX. That is
deliberate: the INDEX enumerates work, a thread file sequences it.
