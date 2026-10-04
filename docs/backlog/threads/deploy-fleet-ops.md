# deploy-fleet-ops

## Resume

- **Pillar:** platform
- **Next:** When activated, audit the vault-truncation blast radius before lower-ranked deploy work.
- **Blocked by:** No declared active owner; verify the incident premise against deployed state.
- **Unblocks:** Deployments without silent partial configuration.
- **Acceptance:** Use [melchior-vault-truncation-blast-radius](../melchior-vault-truncation-blast-radius.md) and the verification steps in [Do next](#do-next); check current deployment and worktree state before acting.
- **Worktree:** `unified-yawning-rossum` (the deploy session)
- **Builds:** Not estimated here; use the owning item's current slice estimate.
- **Detail:** [Ranked work](#do-next) · [Horizon](#horizon) · [Coordination map](INDEX.md).

## Thread context

Current declared activity: [fleet roster](../../../.claude/fleet/threads.tsv); dated allocation decisions below are historical.

**Status:** ends when a deploy is verifiable, concurrent-safe and leaves no
stale daemon, secret hole or untracked cluster residue behind — the path that
puts every pillar's code on the fleet (`docs/roadmap.md` platform bucket).
Today thirteen filed items have no owner; the order is what a deploy can
silently get wrong (secrets, lock, assertions) first, then build and node
residue, then batched ops and flip steps.
**Last reviewed:** 2026-10-02
**Worktree:** `unified-yawning-rossum` (the deploy session)
**Allocation decision (historical):** no — opens at the next session restart if Reto names it.

## Do next

1. **backlog/melchior-vault-truncation-blast-radius.md** — which deploys ran
   without 14 vault secrets 18–22 Aug; silent partial config is the worst
   deploy failure, and the answer scopes any residue.
2. **backlog/deploy-concurrency.md** — a deploy lock two people can hold;
   two deploys interleaving renders corrupts the fleet state.
3. **backlog/deploy-assertions.md** — verification guards (bounce coverage,
   plist drift, model-serving); makes 1 and 2 detectable after the fact.
4. **backlog/deploy-tree-lints-reachability-and-dest-collision.md** — lint
   unreachable role edits and colliding render targets before they ship.
5. **backlog/deploy-drain-wait-is-a-silent-noop.md** — the drain is fixed;
   the residual is re-reading historical job deaths around deploys.

## Horizon

1. **backlog/data-node-worker-daemon-cleanup.md** — deploy removes the venv
   but leaves the launchd daemon on the data node.
2. **backlog/cluster-daemon-user-model.md** — the hermes vs deploy user
   split bites the container cutover; sequence with the factory container
   items.
3. **backlog/deploy-async-task-controller-filenotfounderror.md** — an
   agent-image build task died mid-poll and retries never ran.
4. **backlog/agent-image-build-stalls-on-mirror-fallback.md** — hour-plus
   image builds, probably not a stall; retitled, verify.
5. **backlog/ansible-pipelining-twins-hang.md** — pipelining hangs
   intermittently against the DGX twins.
6. **backlog/dark-features-activation.md** — flip steps for shipped-dark
   features recorded nowhere else.
7. **backlog/prod-ops-one-offs.md** — batched small cluster-ops sweeps.
8. **backlog/coalescing-deployer-for-many-sessions-one-fleet.md** — coalesce
   deploys only if the burst still hurts after 2.

## Parked

- (none)

## Seam

`session-mcp-shared-server` shares `scripts/deploy` and the ensure script
(its Horizon 3, `backlog/mcps-venv-deploy-gaps.md`); that thread owns the
MCP server's deploy shape, this one the script and fleet. `plugin-split`
owns gr457894 on the same script. `ship-gate-ci` shares the live render tree
with its Do-next 3. `local-backup-pickup`, `catpath-wheel-version-reuse` and
`april-corpus-nas-migration` were cut from this cluster (personal, chemistry,
ingest-and-fetch).
