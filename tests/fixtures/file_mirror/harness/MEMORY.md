# Repo-dev memory

## Ship and gate

- [Read the failure above the cross mark when the ship gate goes red](ship-gate-red.md)
- [The local gate queues concurrent pytest runs on VM-derived slots](gate-slots.md)
- [A flaky test is rerun once, then filed as a gripe with its seed](flaky-rerun.md)
- [A new migration takes main's highest number plus one at landing time](migration-numbering.md)
- [Audit the file list of a rebased squash commit before landing](squash-audit.md)

## Conventions

- [Timestamps are UTC and labelled with Z](utc-timestamps.md)
- [Embeddings are filled by the worker, never by ingest](embedding-worker.md)
- [Cluster addresses and vault blobs are gated across the whole public tree](secret-gate.md)

## Cluster

- [The production web UI is reached over the tailnet serve host with basic auth](prod-web-ui.md)
- [Worker sessions launch in tmux windows named after the worktree](tmux-launch.md)
