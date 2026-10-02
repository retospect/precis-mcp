---
status: idea
pillar: 3d-design
---

# se 3D viewer: the nightly browser check never sees an atomic design

IDEA (2026-09-29), filed from the live verification of the id/name path
unification. The nightly half is BUILT and green:
`.github/workflows/viewer-check.yml` runs `scripts/viewer_check.py probe`
over the committed `tests/fixtures/viewer_check/unicycle-c1.ops.json`
(11 checks; first scheduled run green 2026-10-02 08:58Z), and
`scripts/main-ci-status` reports it.

**Open: the atomic overlay has no nightly coverage.** The unicycle has no
structure-bound block, so `probe` skips its target-surface checks and
never builds the atomic overlay, and `viewer_check.py strain` (the strain
layers: checkbox, threshold slider, top-5% count, θp/120° switch,
pentagons vs the default θp threshold — 13 checks) has only been run by
hand, against prod's `hexa-smooth-drum-v2` through
`scripts/guide-web --db prod`. Closing it needs a second fixture: a small
sp² structure with pentagons (a capped tube or a C60 inside an envelope)
plus the se ops that bind it, seeded by `viewer_check.py seed` — which
today seeds se ops only, so it also has to replay a `structure` design.
Then the workflow runs `strain` over it beside `probe`.

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

Decided (Reto, 2026-10-01): its own nightly workflow on GitHub-hosted
runners (free for a public repo), separate from `check.yml` and scheduled
away from its 05:23 UTC nightly, which already takes 18 of the account's
20 job slots; plus `workflow_dispatch` for an on-demand run. Fixture = the
`unicycle-c1` design's ops. Not against prod: CI would have to hold the
Basic credential, and a prod run tests the deployed tree after the fact.
Runs entirely on the runner, the way `check.yml`'s shards already do:
a `pgvector/pgvector:pg17` service, `uv sync --all-extras`, migrate,
seed the committed fixture, serve precis-web on localhost, and install
Chromium with `playwright install --with-deps chromium` (~150 MB) rather
than pulling the ~2 GB Playwright image. Expected cost: one
4-vCPU/16 GB runner, ~3–5 min a night, CPU-bound (SwiftShader software
WebGL).

## The case, in one affordance (2026-10-01)

`explode` was dead from the feature commit that introduced it until
2026-10-01. Clicking it built the position tracks, showed the vendored
transport bar, flipped the label to "un-explode" — and moved nothing. The
4th argument of `initAnimation(duration, speed, label, repeat)` is the loop
mode, not autoplay, and nothing in it ever calls `play()`.

Everything that normally catches a defect said it was fine. The suite was
green. The console was empty. The server-side offsets were correct (20 of
them, magnitude ~3.2 against a 1 m machine). The button's own label was
right at every step, including after a level change. A reviewer reading the
handler sees a plausible call into a vendored API with a sensible-looking
boolean.

What found it was a canvas pixel diff, and only because the diff was
cropped. The whole-canvas number was n=2479 — a healthy-looking delta that
is entirely the transport bar appearing in a 248x35 strip along the bottom
edge. Crop that strip and sample every 300 ms for 4.2 s: n=0 at every
sample. Driving the vendored transport's own play button instead moved the
model at once (n=22180 rising to 33840), which is what separated "the
trigger is missing" from "the tracks are wrong".

Three things that lane has to inherit from this, beyond the requirements
below: **crop the vendored chrome before diffing** (a strip of UI that
appears on interaction reads as a passing delta), **sample animations as a
series** (one shot at one moment cannot tell "never played" from "played and
looped back"), and **labels are not evidence** (every label in this
affordance was correct while it did nothing).

## Requirements the hand-built harness has already proven it needs

### A phase waits on an observable condition, never on a timeout

Non-negotiable, and paid for twice. The hand harness settled each phase
with a fixed `SETTLE_MS * 3` = 2.7 s. On the 5-block fixture it was tuned
on, that is plenty. On a copy of prod's 20-block `unicycle-c1` a level
change takes ~3.5 s — 1.2 MB of scene out, 660 KB back, then a full
`viewer.clear()` + `render()` — so every phase-d reading was taken before
the swap landed. It reported a zero pixel delta and an un-updated URL with
an empty console, and gr458329 was filed on a swap failure that did not
exist; the correct measurement is 43 tree rows -> 32 and 643 changed
pixels, both arriving at ~3 s.

A fixed wait does not merely risk a flake here. It fails SILENTLY and in
the direction that looks like a bug, and it scales the wrong way: the
bigger and more real the design, the more likely the harness lies. So each
phase polls for the effect it is testing — a changed tree row set, a URL
that carries the new parameter, a settled busy mark — with a generous
timeout, and fails on the timeout rather than on a single late sample.

### A phase whose round trip is a net no-op needs a control shot

Phase b asserts that container mode `hidden` SURVIVES a tree toggle, by
diffing the shot before the toggle against the shot after an off-then-on
round trip and expecting zero. Off-then-on is a net no-op, so a canvas that
rendered NEITHER click returns the same zero — the pass and the blind spot
were indistinguishable for as long as the phase existed. Shooting between
the two clicks fixes it: that intermediate pair must be non-zero
(measured 2026-10-01: n=6872, against n=0 for the round trip) before the
zero afterwards can be read as "survived".

Generalises past this phase: any assertion of the form "X is still true
after Y" needs separate evidence that Y happened at all.

### A clean console is not evidence of an applied change

Selection highlighting called a vendored setter that accepted the colour,
threw nothing, and changed no material — dead on every design, invisible
to everything but a pixel diff. And when it DID log errors (for the two
part kinds it rejects), those errors looked like the cause and were not:
silencing them left the canvas exactly as unchanged. The witness for "did
the picture change" is the picture.

### A numeric claim needs a witness that does not share its arithmetic

The scale bar's label and its own pixel width always agree with each other
— they are computed from the same camera read. Checking it means a second,
independent measurement: the model's pixel extent from the image, against a
known real dimension of the design. Self-consistency across zoom catches a
missing zoom term; only the independent witness catches a bar that is
consistently off by a decade.

### The noise floor is measured in the same run, before anything else

Already observed, kept here because it is the other half of trusting a
null: two shots with no interaction must diff to `bbox None, n=0`. A diff
pass that cannot produce zero on an unchanged pair cannot be read as
evidence when it produces zero on a changed one.

### Canvas AND tree, because they fail independently

The 11 blocks that drop between `refined` and `interfaces` on
`unicycle-c1` are ~5 mm fasteners on a 1 m machine at a 315x469 canvas.
They are near the edge of visible: the real delta is 643 pixels in a
48x125 region, which a slightly different camera or canvas size could
easily take to zero. The treeview row set is the robust witness for "did
the scene swap" and the canvas is the witness for "does it look right" —
a harness with only one of the two has a blind spot the other covers.

Owner `src/precis_web/static/blocktree-3d.js`, `tests/precis_web/`,
`scripts/guide-capture`.
