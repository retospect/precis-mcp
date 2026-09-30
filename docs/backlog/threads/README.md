# Thread files — the ordering layer over `docs/backlog/`

`docs/backlog/` holds 400+ items. It answers "what is open?" and cannot
answer "what do I do next, and why that?" — an index sorted by status and
priority flag does not know that one item silently corrupts data while
another is a convenience, or that three items share a root cause.

A **thread file** answers that, for one thread of work. It is a short,
ordered list of **pointers with a rank rationale**. It owns order and
nothing else.

## The one rule

**Pointers only. Never content.**

If you are explaining what an item *is*, you are writing in the wrong
file — that belongs in the item (`docs/backlog/<slug>.md`) or the gripe.
A thread entry says where the thing is and why it sits at that rank, in
one line. This is the rule that keeps the ordering layer from becoming a
second backlog, which is the failure mode it exists to prevent.

## Shape

One file per thread: `docs/backlog/threads/<slug>.md`.

```markdown
# <thread name>

**Status:** <one line: what this thread is driving at>
**Last reviewed:** YYYY-MM-DD

## Do next

1. **<pointer>** — <why this rank: what it unblocks, or what it prevents>
2. **<pointer>** — <…>

## Parked

- **<pointer>** — <the condition that unparks it>

## No action needed

- **<pointer>** — <ruled / not a bug / already fixed, pending close>
```

A pointer is one of: `backlog/<slug>.md` · `gr<id>` · `td<id>` ·
`qu<id>` · a named deliverable not yet filed (file it before it can be
ranked above the parked section).

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

## Lifecycle

- **Create** a thread file when a thread has three or more live items.
  Below that the items carry themselves.
- **Delete** entries on ship, in the same commit as the ship. The file
  shrinks as the thread closes.
- **Retire** the file when `## Do next` empties.
- **Split** the thread if the file passes roughly 40 lines. A list too
  long to hold in the head is a list nobody re-ranks.

Thread files live in a subdirectory, so `scripts/docs-index` (which globs
`docs/backlog/*.md`) does not fold them into the item INDEX. That is
deliberate: the INDEX enumerates work, a thread file sequences it.
