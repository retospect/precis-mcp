# Updating the cluster deploy overlay on the agent host

**When.** You changed the gitignored overlay (`deploy/inventory/`: real
`hosts.yml` / `topology.yml` / `host_vars` / `group_vars` plus the
vault-encrypted `vault.yml`), or a deploy stopped with `overlay-sync: REFUSE`.
The overlay cannot ride precis-mcp's public git — it is secret and the leak
gate forbids real node names in tracked `deploy/` files. Deploy mechanics:
[`cluster-deploy`](./cluster-deploy.md).

## Shape

- The agent host has a **dev** precis-mcp checkout (user = the controller's login,
  separate from the `deploy`-user venv).
- `git_deploy_helper` in that user's home is a **non-bare** git repo with
  `receive.denyCurrentBranch=updateInstead`; the dev checkout's
  `deploy/inventory` is a **symlink** to it.
- On the controller (Mac), `deploy/inventory/` is its own git repo with remote
  `deploy-helper` → that agent-host path over ssh.
- The factory's copy (`precis-fix-repo/deploy/inventory` on the agent host, used by
  the deploy user's `scripts/release --deploy-only`) is provisioned as a
  `.git`-less file copy, refreshed from the controller's overlay by every
  controller deploy. The factory cut and deploy preflights refuse that copy
  (gr700433). Set `PRECIS_OVERLAY_DIR` to the clone or place it at the main
  checkout's `deploy/inventory`. The clone needs the `deploy-helper` remote
  and a sibling `.vault-pass`.
- `.vault-pass` is hand-carried once, out of band, to
  `precis-mcp/deploy/.vault-pass` (chmod 600). It sits *beside* `inventory/`,
  never inside the overlay repo, so the key never travels in git. Re-carry only
  on a vault re-key.
- If the first push into an unborn branch ever fails to check out, fall back to
  a bare repo + working clone.

## Keeping the two copies in sync (automatic, gr680328)

Every `scripts/deploy` and every `scripts/host-ops/play` runs the overlay sync
preflight (`scripts/lib/overlay-sync.sh`) before hashing or rendering anything.
It fetches `deploy-helper/main` and acts on the relation:

| Mac copy vs `deploy-helper/main` | action |
|---|---|
| identical | proceed |
| behind | fast-forward the Mac copy, proceed |
| ahead | push (fast-forward on the helper), proceed |
| diverged | **refuse** — the 2026-11-14 outage shape |
| uncommitted tracked edits, or untracked `*.yml` / `*.yaml` / `*.json` / extensionless files | **refuse** — ansible would render them here and they reach nobody else |

Backup names (`*.bak-…`, `*~`, dotfiles) are not ansible-loadable and are
ignored. A failed fetch (gateway unreachable) only warns, judges against the
last fetched state and pushes or merges nothing. `PRECIS_DEPLOY_ALLOW_OVERLAY_DRIFT=1`
(alias `PRECIS_DEPLOY_ALLOW_STALE_OVERLAY=1`) renders the Mac copy as-is once:
everything is reported, nothing refused or mutated. A plain-directory overlay (no `.git` of its own, no `deploy-helper`
remote) is skipped by the preflight — which is why the factory's `.git`-less
copy deployed unhindered until it went stale (gr700433). Since then
`scripts/release` applies the same two tests itself when it runs under the
factory release pass (`PRECIS_RELEASE_PASS`): a resolved overlay that is
not its own repo with the `deploy-helper` remote **refuses** (exit 75,
"no host was touched") before the CI wait. A missing selected inventory
or its sibling vault key also refuses, even when the launch worktree has
a usable local copy. Hand deploys keep the skip; `PRECIS_DEPLOY_ALLOW_OVERLAY_DRIFT=1` renders the copy as-is once.

## Editing the overlay (from any session, worktree or not)

`scripts/overlay-sync` is the door. It acts only on a directory that is the top
of its own git repo — never on the code checkout — and prints names, shas and
counts, never file contents or a vault value:

    scripts/overlay-sync status                 # relation + uncommitted names
    # edit files under <main checkout>/deploy/inventory (sed/perl/editor)
    scripts/overlay-sync commit -m 'overlay: <what changed>' [path…]
    scripts/overlay-sync push                   # or just deploy: the preflight pushes
    scripts/overlay-sync log                    # both sides: `<` local-only, `>` remote-only
    scripts/overlay-sync diff                   # file names + status vs the remote

The harness refuses `git -C <main>/deploy/inventory …` and Edit/Write under it
from a worktree session (its git must target the worktree; the detail is
withheld over Remote Control, so it looks like a silent failure). Use the
script. **Never clone the overlay into `/tmp` and push from there**: the Mac
copy then lags by the clone's commits and keeps committing on its stale base —
exactly how it forked for eight weeks before the 2026-11-14 outage.
`updateInstead` checks the push out into the agent host's worktree, so the symlink
reflects it immediately.

## ⚠ `remote rejected … (Working directory has unstaged changes)`

The preflight reports this as `the push was rejected`. `updateInstead` refuses
on **any** dirty tracked file — not only files the push would touch — so one
stale in-place edit on the agent host jams the whole channel silently (it sat 4 days
once). Someone edited `git_deploy_helper` directly instead of pushing from the
Mac. Resolve:

1. List the dirty files on the agent host (`git status`, `git diff` over ssh).
2. For each: if it holds real config that exists nowhere else (e.g. a
   `host_vars` flag), **adopt it into the Mac overlay first**, then
   `git checkout -- .` on the agent host; if it is a stale/damaged copy, revert it.
3. Push again (`scripts/overlay-sync push`, or re-run the deploy).

**Diagnosing a dirty vault file: compare DECRYPTED content, never ciphertext.**
ansible-vault re-encrypts with a fresh nonce on every save, so a changed
ciphertext proves nothing. Run
`ansible-vault view --vault-password-file deploy/.vault-pass <f>` on both
sides, parse, and diff **key sets** — never print values. In the incident this
turned "someone edited secrets, don't touch it" into "the live file is a
strict subset (truncated to about half the keys), git HEAD and the user's own
backup agree byte-for-byte, reverting is restorative". Leaving it alone would
have left ~14 keys missing indefinitely. Agent-host deploys using that
overlay could omit them from rendered configuration where referenced.

## Agent-host dirty check

Playbook `18-node-exporter.yml` installs a five-minute LaunchDaemon on the
macOS gateway when it finds one `/Users/*/git_deploy_helper` directory. Run
`08-monitoring.yml` to load the alert rules. Check
`precis_deploy_overlay_status` in the gateway's node_exporter metrics after
installation. It is 0 for clean, 1 for tracked changes, and -1 if the probe
cannot read the checkout. The probe runs Git as the checkout owner and
publishes neither filenames nor values. `DeployOverlayDirty` covers staged
and unstaged tracked changes. `DeployOverlayCheckFailed` covers probe errors,
stale metric files, and a missing metric on the gateway. The deploy preflight
still checks branch divergence and untracked Ansible-loadable files.

## The overlay needs an off-cluster backup

The overlay's only remote is a cluster node (`deploy-helper`), and
`.vault-pass` is hand-carried. Lose the controller and that node together and
the inventory, vault and node coordinates are gone, which is also what a
database rebuild needs first
([`prod-db-rebuild-from-backup`](./prod-db-rebuild-from-backup.md)). Keep a
copy of `deploy/inventory/` and `deploy/.vault-pass` outside the cluster (the
vault file is encrypted; the pass file is not, so store it separately).
