# security-hardening

## Resume

- **Pillar:** platform
- **Next:** When activated, verify credential-rotation scope and coordinate the required cluster pause.
- **Blocked by:** No declared active owner; confirm current credential state without exposing secrets.
- **Unblocks:** Enforced credential, role and sandbox boundaries.
- **Acceptance:** Use [rotate-credentials](../rotate-credentials.md) and the verification steps in [Do next](#do-next); check current deployment and worktree state before acting.
- **Worktree:** `security-hardening`
- **Builds:** Not estimated here; use the owning item's current slice estimate.
- **Detail:** [Ranked work](#do-next) · [Horizon](#horizon) · [Coordination map](INDEX.md).

## Thread context

Current declared activity: [fleet roster](../../../.claude/fleet/threads.tsv); dated allocation decisions below are historical.

**Status:** ends when no credential that has leaked is still live, no
untrusted input reaches a privileged agent unscanned, and the role and
sandbox boundaries hold under the real pool and profiles — what lets agents
and a public web surface run without a blast radius (`docs/roadmap.md`
platform bucket). Today eight filed items have no owner; the order is a live
leak first, then boundaries that are inert today, then dependency and
residual items.
**Last reviewed:** 2026-10-02
**Worktree:** `security-hardening`
**Allocation decision (historical):** no — opens at the next session restart if Reto names it.

## Do next

1. **backlog/rotate-credentials.md** — leaked agent_rw, OpenRouter key,
   OAuth token and anki password are live today; needs a deliberate cluster
   pause for agent_rw, so it is also a coordination item.
2. **backlog/vault-reveal-cache.md** — the OpenRouter key is revealed
   ~22k/day because the 60 s cache is not holding; every reveal widens 1's
   exposure.
3. **backlog/agent-deny-lists-are-profile-dependent.md** — the dream-agent
   tool deny-list is inert under the command MCP profile; a boundary that
   reads as enforced and is not.
4. **backlog/db-role-enforce-pgbouncer.md** — SET ROLE enforcement breaks
   under the transaction pool; the read-only-agent guarantee depends on the
   answer (shared with session-mcp-shared-server Horizon 1).

## Horizon

1. **backlog/untrusted-input-injection-scan.md** — prompt-injection scan
   slices 2–4 (slice 1 shipped); after the deny-list gap is real.
2. **backlog/render-sandbox-network-jail.md** — render sandbox phase 2,
   network and filesystem jail.
3. **backlog/pillow-marker-pin.md** — Dependabot pillow alerts blocked on
   marker-pdf's Pillow<11 cap; waits upstream.
4. **backlog/web-basic-auth-users.md** — shipped and live 2026-08-22;
   only residuals remain (failed-auth logging, full CSP, feed-token rotate
   race). Pending Reto's housekeeping ruling on whether the file closes
   (td461205).

## Parked

- (none)

## Seam

`session-mcp-shared-server` owns the MCP's DB-role isolation (its Horizon 1);
4 here is the pgbouncer half of the same boundary. `deploy-fleet-ops` owns the
vault-truncation blast-radius answer that scopes 1.
