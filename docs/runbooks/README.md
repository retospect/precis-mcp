# Runbooks

<!-- docs-index:begin -->

- [`cluster-logs`](./cluster-logs.md) — Reach every node over Tailscale. Bare `ssh <host>` works — `~/.ssh/config`
- [`db-thrash-review`](./db-thrash-review.md) — A recurring pass that asks: *is the prod DB (`precis_prod` on caspar) thrashing —
- [`dead-mans-switch`](./dead-mans-switch.md) — `health_digest` (§D, `docs/proposals/health-watchdog.md`) covers every
- [`elsevier-preview-pdf-remediation`](./elsevier-preview-pdf-remediation.md) — gr162364/gr162363 (2026-07-17): `fetcher:elsevier` fetches returned Elsevier's
- [`fda-grant-review`](./fda-grant-review.md) — **Cadence:** 30 days (advisory nudge: `scripts/fda-grant-review`, surfaced in
- [`gripe-gc`](./gripe-gc.md) — A recurring, **weekly** backstop pass that asks: *are there open gripes that
- [`memory-sibling-repos`](./memory-sibling-repos.md) — A recurring, fully mechanical check: does every `~/work/<name>` /
- [`oom-lockout-hardening`](./oom-lockout-hardening.md) — 1. **sshd is un-killable by the kernel OOM killer** — a systemd drop-in
- [`restart-worker-and-watch`](./restart-worker-and-watch.md) — The `worker` and `watch` daemons are a pair (`com.precis.worker` /
- [`rotate-agent-rw-credential`](./rotate-agent-rw-credential.md) — history, a pasted log).
- [`skill-search-review`](./skill-search-review.md) — A recurring pass that asks one question: *when agents `search(kind='skill',
- [`taproot-chase-enablement`](./taproot-chase-enablement.md) — Turns on the two dark passes shipped in Phase 1 (plan
- [`token-review`](./token-review.md) — A recurring, **local** pass that asks one question: *where are these Claude

<!-- docs-index:end -->
