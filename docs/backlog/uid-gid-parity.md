# uid/gid parity — spark's foreign package accounts + chgrp of existing files

`system_users[]` pins gid == uid (sole exception `deploy: 1001`); the role
never renumbers an in-use id. Converged changed=0 on all six nodes
2026-08-08. Two residuals: (1) spark carries Debian package accounts on our
names (`prometheus` 129/128, `grafana` 128/127, `ollama` group 983 vs 805) —
guard excludes them; remediation is a deliberate chown/chgrp-then-renumber,
but decide first whether those identities are even the same principal
(monitoring is caspar-only). (2) macOS gives new files the PARENT DIR's
group (BSD inheritance), so pinning the primary group fixes nothing on the
Macs — the export tree must be chgrp'd once: NFS `gguf` 806:20, `workspace`
813:20, `media` 806:812 (orphan gid), plus deploy-owned trees on the three
Macs. Group-only, low risk, mechanical.
Owner `deploy/roles/users/tasks/main.yml` + inventory `group_vars/all/main.yml`.
Test: `00-users.yml` zero drift with `users_gid_drift_fatal: true` fleet-wide.
