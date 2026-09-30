---
status: idea
title: six subagents each verified their own slice green and the integrated run was still red — per-slice verification does not compose, and nothing in the workflow says so
pillar: platform
---

# Per-agent green is not integrated green

## What

On 2026-09-28 a session split a change across six subagents, each verifying
its own slice. All six reported green. Running the same areas **together**
surfaced three real failures in `tests/test_design_scrubber.py` that every
individual run had missed: container leaves had gained a `" (envelope)"`
label suffix, and the scrubber asserts on rendered leaf names. The failures
were real defects in the change, not flakes — the expectations needed
fixing.

The slices were individually correct. What no slice owned was the seam
between them: one agent changed how a leaf renders, another owned the test
that asserts on rendered names, and neither run covered both.

## Why this is worth a note

The obvious reading — "just run the full suite" — is already the rule for
`/go`, and it is what eventually caught this. The non-obvious part is that
**six green reports is weaker evidence than one green run**, and the
workflow currently presents them as if they were the same kind of evidence.
A caller who fans out to N agents and collects N greens has verified N
slices and zero seams, but the report reads like coverage.

This is the same shape as the `/qland` → settle-up trade one level down:
individually plausible changes, collectively untested. The repo already
knows this about merges and has a gate for it; it does not say it about
subagent fan-out.

The cost here was small because the session ran an integrated check before
landing, on its own initiative. Had it landed on the six greens, the
failures would have surfaced in someone else's settle-up gate an hour later
— which is exactly what `1a547543` did to the 2026-09-28 gate with the
`precis-se-help` size caps (a 1h38m run spent to discover it).

## Fix — sketch

1. **Say it in the agent-sizing guidance.** `AGENTS.md` §Agent sizing and the
   coder/test-author remits should state that a fan-out's slice greens do not
   substitute for one integrated run over the union of touched areas, and
   that the *caller* owns that run — not any of the agents.
2. **Make the union run cheap to construct.** The integrated check here was
   hand-assembled from six areas. `scripts/test --impacted` over the combined
   diff is the natural thing; worth confirming it does the right thing when
   the diff spans several agents' work and pointing at it explicitly.
3. **Note the ordering trap.** A verification run started before a later fix
   lands describes a tree that no longer exists. The same session correctly
   killed a queued combined run for this reason — the integrated run has to
   be the *last* thing before landing, not something started in parallel with
   the final edits.

## See also

`docs/backlog/orphaned-test-runs-hold-gate-slots-forever.md` — the other
verification gap from the same day; between them they explain most of that
day's lost gate throughput.
