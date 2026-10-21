---
roster: active
pillar: operations
effort: medium
---
# security-hardening

## Resume

- **Next:** Read-only check of which leaked credentials are still live, printing no secret; then a rotation window with Reto (Do next 1).
- **Blocked by:** Nothing for the check; the rotation needs Reto's window and a cluster pause for agent_rw.
- **Unblocks:** Enforced credential, role and sandbox boundaries.
- **Acceptance:** Use [rotate-credentials](../rotate-credentials.md) and the verification steps in [Do next](#do-next); check current deployment and worktree state before acting.
- **Worktree:** `security-hardening`
- **Builds:** Not estimated here; use the owning item's current slice estimate.
- **Detail:** [Ranked work](#do-next) · [Horizon](#horizon) · [Coordination map](INDEX.md).

## Thread context

Current declared activity: the `roster:` front matter above; dated allocation decisions below are historical.

**Status:** ends when no credential that has leaked is still live, no
untrusted input reaches a privileged agent unscanned, and the role and
sandbox boundaries hold under the real pool and profiles — what lets agents
and a public web surface run without a blast radius (`docs/roadmap.md`
Pillar 5 Operations). This thread owns `backlog/rotate-credentials.md`. The
order is a live leak first, then boundaries that are inert today, then dependency and
residual items.
**Last reviewed:** 2026-10-17
**Worktree:** `security-hardening`
**Allocation decision:** active — Reto 2026-10-17 (Pillar 5 Operations).

## Do next

1. **backlog/rotate-credentials.md** — first a read-only check of which of
   the leaked credentials (agent_rw, OpenRouter key, OAuth token, anki
   password) are still live, without printing any secret; then a rotation
   window agreed with Reto, since agent_rw needs a deliberate cluster pause.
   A live leaked credential outranks every boundary below.
2. **backlog/agent-deny-lists-are-profile-dependent.md** — the dream-agent
   tool deny-list is inert under the command MCP profile; a boundary that
   reads as enforced and is not.
3. **backlog/db-role-enforce-pgbouncer.md** — SET ROLE enforcement breaks
   under the transaction pool; the read-only-agent guarantee depends on the
   answer (shared with session-mcp-shared-server Horizon 1).

## Horizon

1. **backlog/untrusted-input-injection-scan.md** — prompt-injection scan
   slices 2–4 (slice 1 shipped); after the deny-list gap is real.
2. **backlog/render-sandbox-network-jail.md** — render sandbox phase 2,
   network and filesystem jail.
3. **backlog/web-basic-auth-users.md** — shipped and live 2026-08-22;
   only residuals remain (full CSP, feed-token rotate race). Pending Reto's housekeeping ruling on whether the file closes
   (td461205).
4. **backlog/multi-user-overhaul.md** — four-role model, per-name vault
   ACL, settings scopes, authenticated human actor; its S1/S2/S4 close the
   HPC D1/D2 and trust Q2 risks accepted 2026-10-21. Draft; Reto's open
   questions first. `backlog/out-of-box-setup.md` (admin users screen)
   waits on its S3.

## Parked

- (none)

## Seam

`session-mcp-shared-server` owns the MCP's DB-role isolation (its Horizon 1);
3 here is the pgbouncer half of the same boundary. `deploy-fleet-ops` owns the
vault-truncation blast-radius answer that scopes 1.
