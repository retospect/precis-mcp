---
id: host-play
family: work
flavor: runbook
title: precis — apply one queued host step by hand, safely
summary: fixed checklist for hand-running a single deploy playbook against the cluster — the pinned play wrapper, dry-run-first sequence, per-play traps (pgbouncer, autocatpath, macOS), durable config homes and overlay discipline
answers:
  - how do I apply a queued host step by hand without breaking prod?
  - why does a bare ansible-playbook from the repo root fail with "role not found"?
  - how do I run the pgbouncer or autocatpath play safely?
  - where do hand-set worker env vars survive a deploy re-render?
  - how do I edit the inventory overlay without clobbering someone else's work?
applies-to: hand-running deploy/playbooks against the cluster from an operator machine
status: active
tags: [troubleshooting, workflow]
---

# host-play — apply one host play by hand

`scripts/deploy` renders and runs only `redeploy-precis.yml`. Every other
play — backups, monitoring, pgbouncer, constraints, a single worker play —
is applied by hand, and each carries a trap that only bites when a human
runs it. This skill is the fixed checklist. Host coordinates live in the
gitignored overlay and repo-dev memory; every host below is a placeholder.

## Run every play through the pinned wrapper

Never invoke `ansible-playbook` yourself for a cluster play. The rule,
verbatim: host-ops/play pins the ref. `scripts/host-ops/play` fetches
`origin/prod`, refuses unless the prod checkout is exactly at that sha
(clean, and past the host-lanes gate), then runs `ansible-playbook` with
the overlay inventory and vault password from the prod checkout's
`deploy/` directory, with `precis_worker_git_ref`, `precis_web_git_ref`
and `precis_embedder_git_ref` set to the prod sha. That pin is the whole
safety story:

- A bare `ansible-playbook deploy/playbooks/<play>.yml` from the repo
  root fails with "role not found" — roles resolve relative to the render
  tree, not your checkout.
- Running from the main checkout instead of the pinned prod one would
  push main's `uv.lock` as the hosts' constraints file — hosts get the
  packages main picked, not the versions prod pins.
- Roles that pip-install precis fail the play when the install requires
  a ref and that ref is unset or empty. For example, `44-autocatpath.yml`
  fails without `precis_worker_git_ref`.
- The wrapper runs the overlay sync preflight first; when it refuses on
  drift, reconcile the overlay rather than overriding.

```bash
scripts/host-ops/play playbooks/<NN>-<play>.yml [ansible args]
```

## Dry-run, compare, apply, verify

1. Dry run: the same line plus `--check --diff` through `play`. Read the
   diff against the change you expect; a diff you cannot explain stops
   the run.
2. Apply without `--check`. This is a prod write — if a permission
   classifier refuses the call, hand the exact line to the deploy owner
   instead of improvising a variant that skips the wrapper.
3. Verify on the host: the unit is active, or the exporter scrape
   answers. A green `--check` does not prove the unit exists — check
   mode reports what a task *would* change, not whether the service
   runs. One play can never pass `--check` at all: `44-autocatpath.yml`
   (its seed tasks always execute), so apply it and verify by the unit.

## pgbouncer drops every client, even at changed=0

The pgbouncer play (`02-postgres.yml --tags pgbouncer`) always unloads
and reloads the launchd job, and the restart handler fires on notified
changes — every pooled client is dropped for a few seconds even when the
run reports `changed=0`. The unload/load pair runs unconditionally, so
never re-run the play just because the check came back clean. When only
the auth file changed, the role's SIGHUP reload handler re-reads it
without dropping connections (gr583700) — that reload, not the restart,
is the cheap path.

## autocatpath needs its wheels passed explicitly

`44-autocatpath.yml` installs a private CUDA wheel stack. Pass the wheel
path explicitly with `-e autocatpath_wheel=<path>` and, where the
topology runs the xtb relax rung, the tblite backend wheel too with
`-e tblite_wheel=<path>` (PyPI ships no linux-aarch64 tblite wheel, so
`scripts/build-tblite-wheel` builds it from the versioned PyPI sdist).
`--limit` the GPU nodes — the play's targeting follows the topology
map, so an unlimited run touches every capable host at once. Build the
autocatpath wheel from the commit the lockfile pins, not from main.
After it lands, verify the worker drop-in and the unit restart it
notifies.

A missing tblite wheel is not a silent skip: the inventory overlay sets
`tblite_required: true` for topologies that pin xtb jobs to these hosts,
and the play then fails outright on any such Linux host whose wheelhouse
holds no `tblite-*.whl` — every xtb job there would fail as infra. The
check is Linux-only; macOS kind-surface hosts take tblite from PyPI.
Push the wheel with the same play (`-e tblite_wheel=<path>`) before
re-running.

## macOS hosts — groups, handlers, plists

- There is no `root` group on macOS; the root-equivalent group is
  `wheel`, so an explicit `group: root` task fails outright, not just
  silently mis-owns the file. `ansible.builtin.file` without an explicit
  `group` makes new files inherit the parent directory's group — on a
  shared ops directory that can be `staff`, not the service account. Set
  `group:` deliberately wherever the ownership matters, keyed on
  `os_family == 'darwin'` to resolve to `wheel`, as
  `deploy/roles/db_exporters/tasks/main.yml` does.
- Same-named Darwin and Linux handlers in one role silently skip the
  second: ansible runs only the first handler matching a notified name,
  so the Linux restart never fired (gr494656). The enforced shape is
  distinct handler names sharing a `listen:` topic, pinned by
  `tests/test_deploy_handler_names.py` — follow it when you add a
  handler.
- `launchctl kickstart -k system/<job>` restarts the loaded job but does
  not re-read an edited plist. After editing the plist itself, use
  `launchctl bootout system/<job>` then `launchctl bootstrap system
  /Library/LaunchDaemons/<job>.plist` — or let a deploy re-render it.

## Hand-set env vars die at the next re-render

A variable hand-edited into a live service definition — an
`Environment=` line, a drop-in file — disappears the next time a deploy
re-renders the unit. The template or the host var is the only durable
home; put it there first. Before a release deploy that re-renders worker
units, diff the live environment against what the templates render
(`systemctl show <unit> -p Environment` against the rendered unit file)
so nothing silently reverts.

## The overlay is a separate, often dirty repo

`deploy/inventory/` (the gitignored overlay) is its own git repo mirrored
to the gateway. When you edit it:

1. Commit only your own block — read the plaintext diff first and confirm
   it names nothing but your change.
2. Confirm the vault file still starts with its vault header — an
   ansible-vault-encrypted file begins with a marked magic line; a
   plaintext file has none. A vault committed or pushed as plaintext is a
   leak, and the header is the cheapest check.
3. When the remote copy is dirty (someone else's unpushed work), do not
   force-push over it: push a side ref and merge on the other side.

The overlay sync preflight inside `play` and `scripts/deploy`
fast-forwards, pushes, or refuses on divergence — treat a refuse as a
queue to reconcile, never to bypass.

## Shell session gotchas

- In an interactive shell, `! cd deploy` leaves the working directory at
  `deploy/` for later commands. From there, repo-root `scripts/` paths fail,
  and a line starting with `cd deploy && ...` tries to enter `deploy/` again
  and fails with “no such directory.” Run `! cd ..` to return to the repo root.
- Ad hoc scripts in `/tmp` vanish on reboot. Ask a cluster-ops agent for
  current host status after a reboot.

## See also

- [[backup-restore-drill]] — the other hands-on prod drill.
- `docs/runbooks/host-ops.md` — the host-ops script reference (`play` itself).
- `docs/runbooks/cluster-overlay-sync.md` — keeping the two overlay copies in sync.
