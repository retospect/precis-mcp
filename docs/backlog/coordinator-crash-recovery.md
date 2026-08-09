# coordinator executor still depends on the wall-clock sweep

Tier-B lease authority shipped (622dd03c) for
ssh_node/claude_inproc/claude_docker; `coordinator` deliberately does NOT opt
into reclaim_stale_running (a crashed slice has no re-claim path of its own)
and keeps the PRECIS_STUCK_JOB_HOURS wall-clock as its only crash recovery.
Give it one, or accept the wall-clock as the permanent backstop. Spec:
`compute-lane-lease-epoch` (git-only) (built). Owner
`src/precis/workers/executors/_common.py`.
