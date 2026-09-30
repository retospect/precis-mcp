---
status: idea
pillar: 3d-design
---

# se 3D viewer: no browser-level check exists for it at all

IDEA (2026-09-29), filed from the live verification of the id/name path
unification.

Every test over the 3D viewer asserts the SHAPE of the emitted scene JSON.
That is exactly the assertion class that let every visibility toggle in the
viewer sit dead behind a fully green suite and a clean console: the JSON was
never wrong, the two key spaces were. Verifying the repair needed a real
browser, a real canvas, and a pixel diff — a one-off harness built by hand
and thrown away (probe + diff scripts left under `.claude/scratch/unicycle-*`
of the worktree that did it; they seed a design from
`tests/precis_web/test_blocktree_view.py::_UNICYCLE_OPS`, serve it out of a
`precis-dev` container, drive it from
`mcr.microsoft.com/playwright/python`, and diff the canvas at >8/channel).

The load-time invariant self-check in `blocktree-3d.js` covers the specific
defect that was found, loudly, at runtime. It does not cover the next inert
affordance of a different shape.

What a durable version would need to decide: where the browser runs (the
gate is container-first and has no browser image today, and the ~2 GB
Playwright pull is not a gate-lane cost anyone wants on every push), whether
it is a nightly lane rather than a per-push one, and what the seeded design
is. `scripts/guide-capture` already solves the "drive a real browser from
this repo" half — the missing half is a design fixture plus canvas diffing.

Owner `src/precis_web/static/blocktree-3d.js`, `tests/precis_web/`,
`scripts/guide-capture`.
