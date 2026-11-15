---
roster: active
pillar: operations
effort: medium
---
# deploy-fleet-ops

## Resume

- **Next:** Audit the vault-truncation blast radius before lower-ranked deploy work (Do next 1).
- **Blocked by:** Nothing; verify the incident premise against deployed state.
- **Unblocks:** Deployments without silent partial configuration.
- **Acceptance:** Use agent-host-vault-truncation-blast-radius (gr690093) and the verification steps in [Do next](#do-next); check current deployment and worktree state before acting.
- **Worktree:** `unified-yawning-rossum` (the deploy session)
- **Builds:** Not estimated here; use the owning item's current slice estimate.
- **Detail:** [Ranked work](#do-next) · [Horizon](#horizon) · [Coordination map](INDEX.md).

## Thread context

Host play owed: after the colima launchd retry template lands, builder-ce or
Reto applies `precis_worker_agent` to the affected worker group and verifies
a failed status force-stops stale lima state before restart.

Current declared activity: the `roster:` front matter above; dated allocation decisions below are historical.

**Status:** ends when a deploy is verifiable, concurrent-safe and leaves no
stale daemon, secret hole or untracked cluster residue behind — the path that
puts every pillar's code on the fleet (`docs/roadmap.md` Pillar 5 Operations).
Today thirteen filed items have no owner; the order is what a deploy can
silently get wrong (secrets, lock, assertions) first, then build and node
residue, then batched ops and flip steps.
**Last reviewed:** 2026-10-02
**Worktree:** `unified-yawning-rossum` (the deploy session)
**Allocation decision:** active — Reto 2026-10-17 (Pillar 5 Operations).

## Do next

1. **gr690093** — which deploys ran
   without 14 vault secrets 18–22 Aug; silent partial config is the worst
   deploy failure, and the answer scopes any residue.
2. **me690552** — a deploy lock two people can hold;
   two deploys interleaving renders corrupts the fleet state. Partially
   landed 2026-10-21 (worktree `bug-deploy-lock`): the atomic steal +
   ownership-checked release and the ancestry-branched fail_msg are done;
   residual is the descendant-nonfatal decision (Reto call) and closing
   the item.
3. **gr690387** — verification guards (bounce coverage,
   plist drift, model-serving); makes 1 and 2 detectable after the fact.
4. **me690563** — lint
   unreachable role edits and colliding render targets before they ship.
6. **Fleet authority v2** — `.claude/fleet/authority.md` v2 and its
   generator, which may land in parallel from the coordinator's tree; it
   names who may deploy, so once it lands this thread keeps the deploy
   roles in it current.

## Horizon

0. **gr692107** — machine kind slice 2: source
   capability facts (GPU, worker lanes, services) from deploy config /
   service_config / worker lanes instead of hand-entered args. Slice 1
   (kind + refs + heartbeat overlay) landed (a9e94c40b); the item names
   the open decision (no inventory reader exists in `src/` yet).
1. **gr690328** — deploy removes the venv
   but leaves the launchd daemon on the data node.
2. **gr690238** — the hermes vs deploy user
   split bites the container cutover; sequence with the factory container
   items.
3. **gr690411** — an
   agent-image build task died mid-poll and retries never ran.
4. **me691271** — hour-plus
   image builds, probably not a stall; retitled, verify.
5. **gr690167** — pipelining hangs
   intermittently against the DGX twins.
6. **gr690306** — flip steps for shipped-dark
   features recorded nowhere else.
7. **me690922** — batched small cluster-ops sweeps;
   now also the 2026-10-10 credential/service gripe batch (the agent host OAuth
   token, mcp-http prod clone, a worker host openclaw).
8. **gr690257** — coalesce
   deploys only if the burst still hurts after 2.
9. **gr690534** — prod-psql prompts on already-approved
   writes, plan_tick without a checkout.
   These stall agents but corrupt nothing, so they rank below the deploy
   work.

## Parked

- (none)

## Seam

`session-mcp-shared-server` shares `scripts/deploy` and the ensure script
(its Horizon 3, gr692199); that thread owns the
MCP server's deploy shape, this one the script and fleet. `ship-gate-ci` shares the live render tree
with its Do-next 4. `local-backup-pickup`, `catpath-wheel-version-reuse` and
`april-corpus-nas-migration` were cut from this cluster (personal, chemistry,
ingest-and-fetch).
