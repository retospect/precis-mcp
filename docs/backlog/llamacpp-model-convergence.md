# llamacpp GGUF fleet convergence has no automation; catalog has drifted

The role's design is sound — caspar downloads canonically into NFS
(`deploy/roles/llamacpp/tasks/download.yml`, writes `manifest.yaml` with
per-model SHA256) → each inference node rsyncs its declared
`host_vars[*].llamacpp_models` subset to local SSD (`tasks/sync.yml`) →
serves local; the catalog is meant to be source of truth. Findings
(read-only, 2026-08-05): **no convergence automation** — sync is a manual
`ansible-playbook deploy/playbooks/04-llamacpp.yml --tags sync`, no
cron/launchd/timer anywhere; last converged 2026-04-24 (all local GGUFs
4.5 months stale). Catalog≠reality, and a blind sync is destructive: spark
declares `llamacpp_models: []` but is hand-edited live to serve the 80B (a
deploy re-render already wiped it once, breaking the quest loop); melchior
holds an undeclared DeepSeek-R1-70B orphan; balthazar matches. **No
runtime verification** — clients never check local files against
`manifest.yaml`'s SHA256, so silent partial-sync/bitrot is undetectable.

Fix path: (1) reconcile the catalog to intent (declare spark's real served
set so converging is non-destructive; resolve melchior's orphan); (2)
converge once (`--tags download` to refresh canonical+manifest, then
`sync,config,service`; verify against manifest SHA256); (3) automate
convergence — a scheduled per-node systemd timer / launchd running
catalog-driven rsync+prune+checksum-verify, nodes self-heal; (4) optional:
a `gguf_check` system-profile precis pass (sibling of `disk_check.py` from
gr191008) raising `kind='alert'` on drift — warn on stale/missing,
critical on served-model-missing or checksum mismatch.

Promoted from gr194396's sibling gr194304.
