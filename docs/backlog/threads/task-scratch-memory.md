---
roster: active
pillar: memory-graph
effort: high
design-review: yes
---
# task-scratch-memory

## Resume

- **Next:** Do next 1 (gr605668) and 2 (gr605669) share one primitive — the
  `scratch-for:<owner>` tag — so one builder takes both, then 3 (gr605672)
  writes the harvest step against them. Builder tier: Opus (Reto
  2026-11-09): every item here lands skill text (`precis-memory-help`,
  `precis-gripe-loop-help`) in the same commit as its code; the skill text
  is the deliverable the cluster agents read, so a mechanical tier does
  not qualify.
- **Blocked by:** Nothing. `SPACE:scratch` and the wall-clock sweep shipped
  2026-11-06 (gr568398, on prod 04920c247).
- **Unblocks:** Multi-agent task scratch that is invisible to the other
  tasks in flight, dissolves when the task closes, and is read once by
  the closing agent before it does — the gripe-fix loop's working notes
  stop leaking into every other builder's recall.
- **Acceptance:** a scratch node with `scratch-for:td<T>` is excluded from
  `+recall` and ring-narrowed fisheye of another task's scratch; it is not
  retired while `td<T>` is open and is retired after its close + grace;
  `get(kind='todo', id=T, view='scratch')` lists it; the loop skill's
  close step names `harvest:`. `tests/workers/test_memory_thread_sweep.py`
  and `tests/test_eye_render.py` green.
- **Worktree:** `task-scratch-memory`
- **Builds:** 2 (tag + expiry + ring in one; harvest view + skill text in
  the second).
- **Detail:** [Ranked work](#do-next) · [Coordination map](INDEX.md).

**Status:** ends when an agent working one task writes its probes, dead
ends and drafts into memory without a cleanup chore, recalls only its own
task's scratch, and the durable residue is one clean node the closer
chose — `docs/roadmap.md` pillar memory-graph. Today the throwaway space
exists but is one shared bucket on a wall clock; the list below is
ordered by dependency (the owner tag first, then what reads it).
**Last reviewed:** 2026-11-09
**Worktree:** `task-scratch-memory`

## Do next

1. **gr605668** — scratch expiry keyed to the owning task's close. First
   because it mints the `scratch-for:<owner>` tag the other two read; a
   wall-clock TTL loses long tasks' scratch mid-flight.
2. **gr605669** — thread-scoped scratch recall (`RingFilter` tags
   dimension, `put(args={'scratch_for': …})`). Same tag; without it
   concurrent builders recall each other's probes.
3. **gr605672** — harvest step at the loop's close (`view='scratch'` on the
   todo, `harvest:` line, `supersedes` from the clean node). Last because it
   reads the tag and the view; without it durable residue dissolves unread.

## Horizon

1. **Gardener detector for unharvested scratch** — a closed owner with
   live scratch and no `harvest:` line is review debt the garden should
   flag (seam with `graph/garden/candidates.py::memory_expiry_shape`);
   file once Do next 3 has run on prod for a wave.

## Parked

- (none)

## No action needed

- (none yet)

## Seam

`workers/memory_thread_sweep.py` is shared with the memory-gardener cluster
(gr560759 thread_shape, knowledge-mesh ranks the gardener); land Do next 1
before any gardener change to the sweep's candidate query, or rebase onto
it. `graph-memory-consumers.md` owns the eye ladder (`RingFilter`); Do
next 2 adds a dimension there and must not change ring renders for
unfiltered calls.
