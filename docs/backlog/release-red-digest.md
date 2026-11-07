---
status: draft
title: Wire the ci-failed digest into scripts/release's red-gate summary
pillar: platform
---

# Wire the ci-failed digest into scripts/release's red-gate summary

## Motivation / why

Review td580800 of ae4e457a8 (td580799): `scripts/ci-failed` now emits a
`failed ids:` roll-up plus the by-design matrix-skip note (a red
`failed-first` leaves the `test-linux` shards `skipped` BY DESIGN — the
run's verdict is the failed ids alone, the read the r236 gate got wrong,
gr580432). But `scripts/release`'s red exit-2 summary still prints only
the failing job names, job URLs, and a `scripts/ci-failed <run_id>`
pointer — the operator/agent still has to run a second command to see
what actually failed, which is the gap the change set out to close.

Blocked on lane authorization: the summary is composed inside
`scripts/release` (`wait_ci` ~L1147-1165, `_wait_fresh_verdict`
~L563-573) and `scripts/lib/ci_run.py`; both sat in slice td580980's deny
list along with every worker/plan consumer, so no allowed seam reaches
that output.

## In scope

`scripts/release`, two red print sites (`wait_ci`, `_wait_fresh_verdict`):

1. When `red["run_id"]` is set, run `scripts/ci-failed <run_id>` as a
   subprocess (capture, timeout ~180 s, best-effort — a digest failure
   must not change the exit-2 verdict) and print its stdout, indented,
   into the stderr summary before the existing pointer line. That one
   run carries both finding items: ci-failed already prints
   `run_diagnostics` + the matrix-skip note, then the per-job digest with
   `failed ids:`.
2. Keep the `scripts/ci-failed <run_id>` pointer line (the digest's own
   doc points to `gh run view --log-failed` for the full log); on digest
   failure print the pointer as the only read.
3. Same wiring in `_wait_fresh_verdict`'s red print block (it already
   loops `red["diagnostics"]`; the subprocess digest rides the same
   `red["run_id"]`).

No `scripts/lib/ci_run.py` change needed: the digest is reused by
subprocess, not by moving `_matrix_skip_note`/`_failed_ids` into the lib.
(The runpy alternative — `_CIF = runpy.run_path(str(HERE / "ci-failed"))`
plus `_CIF["_matrix_skip_note"](run)` appended to `diagnostics` in
`completed_run_red` — gives the note but NOT the ids, which need per-job
log reads; the subprocess gives both for ~15 lines.)

## Explicitly NOT in scope

- `scripts/ship`, `scripts/round`, `scripts/main-ci-status` red lines —
  same gap, different latency trade-offs (hook/status paths); separate
  item if wanted.
- `scripts/lib/ci_run.py` refactors; `scripts/ci-failed` itself (already
  landed).
- The plan-side digest path — `ci_red_digest` already posts the digest
  as a `brief:` on fix todos; unchanged.

## Acceptance criteria

- `scripts/release` exit-2 stderr on a red head names the failing node
  ids (`failed ids` roll-up or the FAILED lines) and, for a red
  `failed-first`, the `test-linux shard job(s) skipped by design` note.
- `tests/test_release_script.py`: extend the `gh` stub in
  `tests/test_round_gate.py` (`_GH_STUB`) to answer `gh run view <id>
  --json status,conclusion,jobs` and `gh run view <id> --log-failed`
  from `$GH_STUB_DIR` files, then assert the red stderr carries the ids
  and the skip note; existing assertions (`scripts/ci-failed 4242` in
  stderr, branch left in place) still hold.
- Digest subprocess failing/timing out still exits 2 with the pointer
  line; never 0.

## Target + blast radius

- `scripts/release` — the two exit-2 stderr blocks only.
- `tests/test_release_script.py`, `tests/test_round_gate.py` stub.
- Readers: `/go` operators, `release_cut`/`release_deploy` job logs, the
  release-fix loop's humans. Verdict recording, deploy path, gate
  semantics untouched.

## Open questions / decisions log

- Decided: subprocess reuse of `scripts/ci-failed` over moving helpers
  into `scripts/lib/ci_run.py` — one code path to maintain, the lib
  stays untouched, and only the subprocess yields the failed ids.
- Decided: keep the pointer line — the digest is a summary, the command
  remains the route to the full log.
