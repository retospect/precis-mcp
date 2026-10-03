---
status: ready
pillar: platform
title: the docker VM disk fills (90 GB build cache, ~40 idle test/gate containers) and every lint, gate and scripts/test dies on a raw ENOSPC with no test output
---

# Docker VM disk fills silently

## What

On 2026-10-03 the colima VM disk reached 98%. The build cache had grown to
90 GB, 40 GB of it unused, and about 40 test and gate containers had been up
for 1–3 h. The uv cache then hit ENOSPC, so the pre-qland lint, gates and
`scripts/test` failed with no test output, which reads as a red gate. The
orchestrator pruned the build cache by hand (79%, 41 GB free).

Nothing caps the build cache, and nothing checks free space before a run
starts.

## Fix

1. **Preflight disk check.** `scripts/test` and `scripts/ship` (gate and
   pre-qland lint) read the VM's free space before they start, e.g.
   `docker system df` or `df` inside a throwaway container on the docker
   root. Below ~10 GB (`PRECIS_DOCKER_MIN_FREE_GB`) they refuse with a named
   message: "docker VM disk has N GB free; run scripts/reap-test-dbs, or ask
   the orchestrator to prune; do not re-run". A check that cannot read the
   number warns and proceeds; it never refuses on an unknown.
2. **Build-cache cap in the reaper.** `scripts/reap-test-dbs` (or the
   SessionStart reaper that calls it) runs
   `docker builder prune -f --keep-storage <cap>` with the cap at ~30 GB
   (`PRECIS_BUILD_CACHE_KEEP_GB`), at most once an hour, guarded by a stamp
   file.
3. **Idle containers.** The ~40 containers up 1–3 h: identify which kind
   they are (gate warm containers, `compose run --rm` leftovers, agent-tree
   dbs that predate the teardown in ae5084ae3). Extend the reaper only for a
   kind that is provably idle.

## Acceptance criteria

- With free space under the threshold, `scripts/test` and the pre-qland
  lint exit non-zero within seconds and print the named message, not
  ENOSPC.
- The reaper keeps the build cache at or under the cap. One run on a host
  over the cap brings it under; the stamp limits it to once an hour.
- Tests drive both with a fake `docker` on PATH, as
  `tests/test_scripts_test_agent_teardown.py` does.
