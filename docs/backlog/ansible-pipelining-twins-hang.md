# Ansible pipelining hangs intermittently against the DGX twins

**Status:** open · found 2026-08-29 during the GPU-compute rehome

**Symptom.** With `pipelining = True` (`deploy/ansible.cfg` `[ssh_connection]`),
plays against castor/pollux intermittently hang forever mid-task: the remote
become-wrapped `/usr/bin/python3.12` sits in S-state polling stdin for the
AnsiballZ payload that never arrives (43-aizynth / 46-alphafold runs sat
5–15+ min per task; identical plays completed in seconds once pipelining was
off). macOS hosts unaffected; the twins are affected *intermittently*, which
is worse — `scripts/deploy` (redeploy-precis.yml) uses the same cfg, so a
full fleet redeploy can wedge silently on a twin with no error output.

**Workaround (verified).** `ANSIBLE_PIPELINING=False ansible-playbook …` —
module transfer falls back to scp; cost is per-task overhead only. Try this
before any deeper debugging of a "hung deploy" on castor/pollux.

**Candidate fixes** (pick one):
- `ansible_pipelining: false` scoped to the twins via
  `inventory/group_vars/serving.yml` (smallest blast radius);
- flip `pipelining = False` fleet-wide in `deploy/ansible.cfg` (simplest,
  costs deploy time on every host);
- root-cause the twins' sshd/sudo interaction (payload-over-stdin race —
  sshd MaxSessions? `sudo -S -n` stdin handling on their Ubuntu build).

Related but distinct: gr256065 (deploy failure *reporting*), the
2026-08-29 caspar NFS lockd wedge (memory `caspar-nfs-lockd-wedge` — that
one D-states on the mount; this one S-states on stdin with a healthy mount).
