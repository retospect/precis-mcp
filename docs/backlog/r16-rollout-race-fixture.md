# Make the R16 rollout race fixture prove its injected race

The R16 release-head-during-rollout test can hide a failed race hook: the
deploy stub ignores its status, and the `_deploy` helper replaces an initial
exit 3 with fake runtime confirmation. Make the test fail distinctly when the
hook does not move the isolated bare origin, and assert the remote branch
actually advanced before checking the release guard.

Acceptance:

- The deploy stub exits nonzero when its configured hook fails.
- A regression injects a failing hook and proves it cannot reach fake
  confirmation, tag, forward merge or retirement.
- The rollout-race test uses the raw first `rig.round("deploy", ...)` result,
  asserts the remote release head equals the injected descendant, then checks
  refusal, retained deployment pin and no deployed tag or retirement.
- No production release script or guard changes; no sleeps or weakened checks.
- Focused canonical tests and scoped container types pass.

This fixture correction addresses the observability gap exposed by frozen
R16 `bd3956d7b661eebe68b9ee68c61ec0a563c390a5`; it does not presume whether
that run also exposed a product race defect. The owner must stop and report if
the corrected fixture shows the release guard accepting an observed changed
head.
