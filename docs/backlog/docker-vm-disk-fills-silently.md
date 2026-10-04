---
status: ready
pillar: platform
title: idle precis-gate and precis-test-db containers of sessionless trees stay up for days and pin old images on the docker VM disk
---

# Docker VM disk: what is left

On 2026-10-03 the colima VM disk reached 98% (90 GB build cache, ~40 idle test
and gate containers), and every lint, gate and `scripts/test` died on a raw
ENOSPC. Shipped: `scripts/lib/docker-disk.sh` (`docker_free_gb`,
`docker_disk_preflight <who>`; refuses with exit 3 below
`PRECIS_DOCKER_MIN_FREE_GB`, default 10; `PRECIS_DOCKER_DISK_CHECK=0` skips),
called from `scripts/test` and `scripts/ship`; and the hourly build-cache prune to
`PRECIS_BUILD_CACHE_KEEP_GB` (default 30) in `scripts/reap-test-dbs`.
Tests: `tests/test_docker_disk_guard.py`.

Not in scope: a monitoring alert at >85% disk use on the colima VM. That
belongs to local-compute's cluster layout pilot (review item
local-compute-17).

## Fix

1. **Idle containers.** Measured 2026-10-03 22:21Z, 53 running: 21
   `precis-gate` warm containers (11 up 26-35 h, 8 up under 40 min, 2 up
   ~40 min), 26 `precis-test-db` (5 are `agent-*` trees under 30 min old; 16 up
   26-36 h, 1 up 10 h, 4 under 45 min), 2 `compose run` containers, `precis-mcp-http`, and the 3
   `precis-code-search` containers (8 weeks, deliberate). 13 of the gates run
   image id `b4771dda6938`, which is no longer a tagged image (a previous
   `precis-dev` build, ~32 GB, kept alive by those containers). Extend the
   reaper only for a kind that is provably idle: the 26-36 h gate/db pairs of
   trees whose session is gone are the candidates, judged by the same
   inflight session/purpose guard the abandoned-worktree sweep uses.
