---
status: draft
title: sim-harness slices 2-3 — quest-driven automation, writeup draft, container drive path
model: opus
---

# sim-harness — slices 2–3 (automation + writeup + container run)

Slice 1 SHIPPED: `precis sim list/ingest/verify` as plain CLI verbs
(`src/precis/cli/sim.py`, `src/precis/sim/{manifest,registry,ingest,verify}.py`)
— manifest (`precis.sim.yaml` in each sim repo), registry
(`slug → {path, git_remote, manifest, quest}`), findings/CSV projected into
`PRECIS_ROOT/sim/<slug>/` and driven through the prose-ingest walker
(`_ensure_ingested`, idempotent), verify with LLM judge → YAML writeback on a
`precis-verify/<date>` branch + `material`/`citation` mint + quest deed.
Full slice-1 spec + four `ready` review rounds: git history of
`docs/backlog/sim-harness.md`.

## Motivation (context)

Reto's standalone Pareto trade-study simulators (`flyinghose`, `flowsim`,
`lighterthanair`) are islands: nothing keeps their data honest against the
literature or turns a run into a written, cited summary, so a finished sim
rots into an abandoned side project. The durable fix is each sim as a
**quest** precis owns — a standing aim whose recurring watches re-verify data
and re-write the summary as literature and the sim evolve. Slice 1 proved the
verbs; this remainder builds the standing loop.

## Remaining scope

**Slice 2 — quest-driven automation + writeup.**

- A `level:recurring` **watch** (per `glossary.md`) under each sim's quest
  wrapping the shipped verbs — re-run ingest/verify as inputs drift.
  Net-new; deliberately NOT the existing `quest_tick` coordinator loop
  (`src/precis/quest/loop.py`) — a builder must not reach for it by mistake.
- **`writeup`** — compose a `draft` from findings + verified data + cited
  related work; refresh when the producing git SHA or the verified-set
  drifts. Open: graduate the draft to `paper`, or keep a mutable `draft`?
  Lean: `draft` until a human promotes it — a sim summary is living, not
  archival.

**Slice 3 — the container drive path.** One *precise* pinned image per sim
(the sims' deps genuinely conflict: PyVista+OpenGL vs pymoo+scipy vs bare
numpy); a job mints `sandbox_run` `mode:run` with the sim's `image` +
`precis_access:read`; outputs harvest back into the corpus (text → git,
trusted-side push). This is **exactly `sandbox_run`'s `image` param +
harvest contract** — build on it, don't reinvent (design:
`sandbox-run` (git-only) §"Re-run & operationalize"; also
`cluster-scheduling.md` trace 5 / §H). Blocked by the `sandbox_run`
`mode:run` + harvest slices.

**Binary plot ingest** (PNG/VTI/VTU as first-class refs) stays deferred —
no binary-blob `put` exists (`figure` is SVG-text-only, ADR 0057;
`datasheet` is `supports_put=False`); the natural home is a `folder` kind,
which the `sandbox_run` harvest slice produces.

## Decided constraints (carried from slice 1)

- Separate repos; the harness reaches out via the registry. Submodules /
  monorepo rejected (couples release cadence, bloats the tree).
- Repo stays fully reconstructable without precis; precis holds a
  searchable, citable, verified mirror. Never blobify the repo wholesale.
- Verify judge trust: auto-commit only to a `precis-verify/<date>` branch —
  review is the merge, never the sim's default branch.
- Drive-path images are per-sim and pinned; a permissive "unlimited-pips"
  image is for *authoring* a sim, never the drive path.

## Acceptance criteria (remaining)

1. A `level:recurring` watch under the sim's quest re-runs verify/ingest on
   its cadence with no bespoke loop code (§A cadence + §F demand shape per
   `cluster-scheduling.md`).
2. `writeup` produces/refreshes a `draft` citing corpus refs; a re-run with
   no input drift is a no-op.
3. (Slice 3) A `sandbox_run` `mode:run` job with a pinned sim image runs the
   sim, calls precis read-only in-container, and harvests text outputs back;
   container teardown leaves the worker alive.
