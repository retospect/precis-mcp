# uid/gid parity — spark's foreign package accounts

`system_users[]` pins **gid == uid with no exceptions**; the role never
renumbers an in-use id. The `deploy: 1001` exception was retired 2026-08-08
(`groupmod -g 806` + chgrp: spark 94.7k, castor 11.2k, pollux 8.9k files;
the Macs were free — 0 files had reached gid 1001, since macOS gives a new
file its **parent directory's** group, not the creating process's primary
group). The NFS export was chgrp'd to canonical groups in the same pass, so
`gguf`/`media` read `deploy:deploy` and `workspace` `813:813` from a Linux
client instead of `dialout`. Converged changed=0 on all six nodes.

**The one real residual — needs a decision, not a chore.** spark carries
Debian *package* accounts on two of our names: `prometheus` (uid 129, gid
128) owning `/var/lib/prometheus` (944 files) and `grafana` (uid 128, gid
127) owning `/var/log/grafana` (475); `ollama`'s group also sits at 983 vs
pinned 805. The guard excludes all three and reports drift, so spark is
deployable but not parity-clean. Before any chown/chgrp-then-renumber,
**decide whether our 803/804 and the distro packages' accounts are meant to
be the same principal at all** — `monitoring` is caspar-only in the
inventory, so these may simply not belong on spark, in which case the answer
is to drop them rather than renumber them.

**Cosmetic leftovers in the export** (deliberately not touched): five
`drwxr-xr-x` dirs still at gid 20 — `home/openclaw{,/scratch,/.ssh}` and
`home/pgboss` (retired monolith users; these want *deleting*, not
relabeling) and `data/papers`, owned by **uid 503, which has no account on
caspar at all** — an orphan uid worth its own look. On all five, group perms
equal world perms, so the label grants nothing.

Not done and probably shouldn't be: chgrp of the deploy-owned *local* trees
on the Macs. It reads as an obvious completion of the above, but 94.7k of
melchior's 185k `deploy:staff` files are `/opt/homebrew`, which wants group
`staff` so the interactive user can `brew install`, and none of those trees
are NFS-exported — so their group is irrelevant to parity.

Owner `deploy/roles/users/tasks/main.yml` + inventory `group_vars/all/main.yml`.
Test: `00-users.yml` zero drift with `users_gid_drift_fatal: true` fleet-wide.
