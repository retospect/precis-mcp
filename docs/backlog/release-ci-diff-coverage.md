---
status: idea
title: Release CI enforces diff coverage (and feeds the mutation pass) now that /go gates on the release
pillar: platform
prio: high
---

# Release CI enforces diff coverage now that /go gates on the release

Since `/go` became land (`scripts/ship --quick`) → `scripts/release` (cut
`release/r<N>`, gate it on check.yml, deploy pinned), its gate is check.yml's
lint + 6 shards. That gate has no diff-coverage check (changed `src/` lines
must be executed by a test, `PRECIS_DIFF_COVER_MIN` default 90) and records no
per-test coverage contexts, so `scripts/mutate-diff` has nothing to run on.
Both lived only on the local full-gate ship (`scripts/ship --mutate --full`),
which `/go` no longer runs.

What: on `release/**` pushes, collect coverage across the 6 shards (combine
`.coverage` artifacts in a follow-up job), run `diff-cover` against the
release's merge-base with the previous `deployed/r<N>` tag (the release's diff
is everything since the last release, not one commit), and fail the verdict
under the floor. Optionally upload the combined `.coverage` with contexts so
`/go` can run `scripts/mutate-diff` against it.

Owner anchors: `.github/workflows/check.yml` (release lane), `scripts/ship`
diff-coverage block, `scripts/last-gated-main-sha` (verdict rule must then
require the coverage job), `scripts/release`.

test: a release whose diff adds an untested `src/` branch gets a red verdict;
`scripts/release` exits 2 naming the coverage job.
