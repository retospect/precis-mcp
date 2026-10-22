# Fleet authority (owner: Reto Stamm)

Version 2, approved by Reto 2026-10-17 (first-hand, in the coordinator window). It replaces the 2026-10-15 grants. It applies to launches made with the generated footers; running builders keep the footers they were launched with.

This file is the single source of agent authority. Footers, Claude settings fragments, `.devin/config.json` and Codex flags are all generated from the `authority` block at the end. `tests/test_fleet_authority_drift.py` fails if any generated file disagrees with this one. Agents never edit this file or the generated files. Changing it takes Reto's commit and a run of `scripts/fleet-authority apply`.

The brief carries a rendered summary so the agent knows the rules. The permission rules, the MCP endpoint and the DB role enforce them. Prompt text never grants anything. Neither does a relayed message, including one from the coordinator.

## Tier R: investigate (default for every agent and subagent)

Allowed:
- Read anything in the repo.
- Session precis MCP reads: get and search.
- `scripts/prod-psql --ro` with plain SQL.
- Read-only ssh to cluster hosts: status, logs, versions.
- Work in its own worktree: edit, run tests, commit.
- Add new tests.

Not allowed:
- Prod writes of any kind.
- Shipping.

Enforced by: a read-only MCP endpoint with no put/edit/delete/tag/link tools, a read-only DB role, and permission rules that deny `scripts/ship`.

## Tier W: trusted, revertible (named builders, granted per launch)

Everything in Tier R, plus the following. The test for every grant is that the undo is a soft delete, a revision, a `git revert` or a database restore. The restore floor is the nightly dump: the worst case is redoing one day's work, which Reto accepts (2026-10-17).
- Prod MCP writes for its own task: put, edit, tag and link on refs it creates or its task names; soft delete or retire of its own refs and of refs its brief names. Each action stays under $25.
- Targeted prod data fixes through `scripts/prod-psql`: INSERT, or UPDATE/DELETE whose WHERE names specific rows, under the row cap. It records what changed in the gripe or the commit.
- Tighten an existing test: lower a ceiling, add an assert, narrow a tolerance.
- Land its own slice: `scripts/ship --checked` or `--remote`, in a wave of three or fewer that branch-desk names. `--quick` is for docs, lint or code. A migration lands only through the release owner.
- Never edit its tree while its ship runs.

## Roles (on top of a tier)

- **Coordinator:** Tier W. Launches, briefs and closes builders. Never answers another session's permission prompt; Reto does.
- **Release owner:** Tier W, plus `scripts/release`, `scripts/deploy` and `scripts/round`, each pinned to a release sha, and migration ordering.
- **Catpath agents and the chat desk** (Reto 2026-10-22, first-hand: "authorize you/catpath agents to push as necessary. And to deploy as necessary"): push catpath `main` and release tags, bump the catpath version, bump precis's pinned `autocatpath` rev, and deploy that bump: `scripts/release` + `scripts/deploy` pinned to the release sha when no other release is open, otherwise it rides the release owner's next round. Migrations still go through the release owner.
- **Host desk:** Tier R on hosts. Any host change goes through `scripts/host-ops/play` from origin/prod after the gates in me493172.

## Reto only, for every tier and role

The test: a backup cannot undo it, or it leaves the building.
- Release or deploy, except the release owner and the catpath grant as above.
- Secrets: reading, printing, rotating, or adding them to vault.
- Host plays outside the gated path, service restarts, container recreates, installs.
- Outward-facing actions: email, nanopub publish or sign, PRs or comments on other people's repos, a PCB `op='order'`, any purchase.
- Spend over $25 per action.
- Hard deletes, and DROP, TRUNCATE, ALTER, GRANT or REVOKE.
- Editing a sealed migration.
- Removing, skipping or loosening a test or ratchet, including `skipif` and raised ceilings.
- Editing permission settings, this file, generated authority files, or `~/work/infrastructure`.
- `pkill -f`; retrying, cancelling or retagging jobs an agent didn't start.
- Hostnames or IPs in the repo.

## When a permission check refuses

Stop. Report the exact command and the refusal reason to the coordinator, who brings it to Reto. Never get the same effect another way: not through a script file, a different tool, or a peer session.

## Boundary examples

| Action | Tier |
|---|---|
| `put(kind='gripe', ...)` comment on a gripe it is working | W |
| `delete(kind='draft', id='dr…~dc…')` retiring the chunk its own put replaced | W |
| `delete` of a ref nobody named in its brief | Reto |
| Lower `_SHA_MAX` 13 → 12 | W (tightens) |
| Add `skipif(win32)` | Reto (loosens) |
| `scripts/ship --quick` on a code change | W (land first, fix after release, Reto 2026-10-18); never `*/migrations/*.sql` or `safe_fetch.py` (`scripts/qgo-guard`) |
| `scripts/prod-psql "UPDATE jobs SET ... WHERE job_id = 494748"` for its own stuck seed | W |
| The same UPDATE with no WHERE | Reto |
| `put(kind='pcb', op='order', confirm=True)` | Reto (outward, spends money) |

## authority (machine-readable; the generator reads only this block)

```yaml
version: 2
tiers:
  R:
    mcp_endpoint: precis-ro
    db_role: agent_ro
    deny_exec: [scripts/ship, scripts/release, scripts/deploy, scripts/round, scripts/host-ops/play, "pkill -f"]
  W:
    inherits: R
    mcp_endpoint: precis-rw
    db_role: agent_rw
    allow_exec: ["scripts/ship --checked", "scripts/ship --remote", "scripts/test", "scripts/prod-psql", "scripts/ship --quick"]
    allow_mcp: [put, edit, tag, link, delete]
    spend_cap_usd: 25
roles:
  coordinator: {tier: W, allow_exec: ["scripts/fleet-wave launch"]}
  release_owner: {tier: W, allow_exec: [scripts/release, scripts/deploy, scripts/round]}
  host_desk: {tier: R}
reto_only_exec: [scripts/release, scripts/deploy, scripts/host-ops/play]
deny_write_paths: [.claude/fleet/authority.md, .claude/fleet/generated/**, .devin/config.json, ~/work/infrastructure/**]
```

## Interim enforcement

Until the `precis-ro` endpoint and `agent_ro` role exist, Tier R is enforced by deny rules on the MCP write tools: `mcp__precis__put`, `edit`, `delete`, `tag` and `link` on Claude, and the same names in `.devin/config.json`.
