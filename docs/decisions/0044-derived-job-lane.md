# 0044 — The derived-job lane: a job parents on its subject, not a todo

- **Status**: accepted (2026-07-02) · **v1 shipped on `chunk-claims-base-workers`**
- **Deciders**: Reto + agent
- **Builds on**:
  - [ADR 0007 — derived queue, no block jobs](./0007-derived-queue-no-block-jobs.md)
    — the philosophy this ADR extends to cross-host compute: a *derived
    artifact* (embedding, summary, keywords) is filled by an idempotent,
    content-addressed pass and gets **no todo**. A DFT relax is the same
    shape; it only became a `kind='job'` because it needs a GPU node, a
    lease, and sweeper recovery — machinery the derived queue lacks.
  - [ADR 0027 — reparent via a reserved link relation](./0027-reparent-via-parent-link.md)
    — the precedent for expressing a tree/ownership relationship as a
    **link**, not a raw parent-column write. The requester↔job edge here
    is a `requested` link for the same reason: two anchors, one column.
  - [ADR 0043 — the `structure` kind](./0043-structure-kind-atomistic-ir.md)
    — the first consumer: an energy-rung relax with no local backend
    dispatches to the GPU node. 0043 shipped it as a job that **required a
    parent todo**; 0044 removes that requirement.

## Context

`JobHandler.put` enforced *"every job is a child of a todo"* (todo-tree
Slice 5). That single `parent_id` did **two unrelated jobs**:

1. **Ownership / status** — where the job lives, dedups, reports.
2. **Intent / wait** — who asked for it and who blocks on it
   (`meta.auto_check = child_job_succeeded`, resolved by walking the
   parent's child jobs; failure bubbles `child-failed:<id>` onto the
   parent, excluding it from the doable rotation until the owner decides).

For an *intentful* job (`plan_tick`, `fix_gripe`) that conflation is
correct: a human or planner decided to do the work and wants to steer it.

For **derived compute** it is an anti-pattern. A DFT relax (and, next,
PCB route / CAD mesh / draft compile) is idempotent, content-addressed,
and cache-fillable — a build step on a first-class artifact, kicked off
by direct manipulation and polled on the artifact (it even has a §23.16
run-cube, so an identical request is zero-compute). Forcing a todo first
meant either a hard `Unsupported` rejection (what the MCP relax path did —
"needs a parent todo to track it") or auto-minting a throwaway todo (what
the web `/instruct` box did). The auto-mint is the tell: a todo created
only to satisfy an invariant carries no intent — it's ceremony that
pollutes the rotation, the attention view, and the projects dashboard.

## Decision

**A job's parent is polymorphic; the lane is emergent from the parent's
kind — not a declared job-class flag.**

- `JobHandler.put` accepts a parent whose kind is in `JOB_PARENT_KINDS`
  = `{todo, structure, cad, draft}` (via `check_job_parent_exists`).
  - parent kind `todo` → **intent lane** (unchanged: rotation +
    `child-failed` bubble + `child_job_succeeded`).
  - parent kind = an artifact → **compute lane**: the artifact owns the
    job (cache/dedup/status on its own `view='runs'`); it has no rotation
    to enter and no meaningful tag to carry a failure.
- **Two anchors, split.** When an intentful task *asks for* a derived
  build and wants to block on it, it links `requested` (requester todo →
  job; migration 0046, inverse `requested-by`). The job still parents on
  the artifact; the link is the only edge back to the requester.
  - **Wait**: a new `derived_job_succeeded` auto_check evaluator resolves
    the requester when a job it `requested` reaches `STATUS:succeeded`
    (the compute-lane twin of `child_job_succeeded`, which walks children
    the requester no longer has).
  - **Failure**: `bubble_job_failure` sees a non-todo parent and follows
    the `requested` links to tag each requester `child-failed:<job_id>`
    (reusing the existing nursery/attention detection). A pure
    direct-manipulation build with **no** requester has nowhere to bubble
    — the failure is visible on the artifact's runs. That is correct: no
    human loop, no todo.

**Why not a `derived: True` flag on the job_type?** It would be a second,
driftable encoding of a fact the parent pointer already carries. The
distinction is *necessary* (intent and compute have genuinely different
failure ergonomics) but should cost nothing — read the parent kind.

**Why not move relax onto the ADR-0007 derived queue instead?** That is
the most principled long-view (relax *is* a derived pass), but the queue
has no lease / forensics / cross-host dispatch / sweeper recovery. The
job substrate does. Parenting on the artifact gets the compute-lane
semantics at zero new machinery.

## Consequences

- The `structure` relax path no longer requires a todo. `edit(kind=
  'structure', ops=[{op:'relax', fidelity:'dft'}])` dispatches a
  `struct_relax` job parented on the structure. `requested_by=<todo_id>`
  (legacy spelling: `parent_id`) additionally wires the `requested` link
  + arms `derived_job_succeeded` on that todo.
- **Class A → Class B is turnkey and better than before**: a planner tick
  can request a relax and block on it, and — because the job dedups on
  `idem_key=struct_relax:<cache_key>` — two tasks requesting the same
  relax share **one** job, each linking `requested`; a cache hit returns
  synchronously and mints no job at all.
- **Fan-in** (N todos `requested` one shared job) is supported: the
  bubble fans the `child-failed` tag out to every live requester; each
  requester's evaluator resolves independently off the one job's status.
- Draft PDF export and cad stay on their current parents for now (a draft
  already owns a project todo); `JOB_PARENT_KINDS` merely *permits* them
  to adopt the artifact-parent later without a substrate change.
- Not yet done: a web "Relax (DFT)" button (the route has no relax POST —
  the MCP `edit` path is the trigger); moving draft/cad compute onto the
  compute lane.
