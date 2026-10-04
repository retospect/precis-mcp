---
status: draft
pillar: local-compute
---

# MeluXina — vault-backed catpath SSH/Slurm; later LLM placement

Review checkpoint: spec and verified seams only. Stop before implementation
or Slurm submission. User login is confirmed; automation authentication and
project entitlement remain unverified. Canonical integration spec consolidating
communicator's draft and the chemistry/local-compute ownership boundary.

Owner: meluxina; Precis orchestrates credentials/jobs/graph outcomes; a generic
SSH/Slurm layer owns remote lifecycle; catpath is its first workload adapter.
Implementation worktrees:
`codex-meluxina`, `codex-catpath-meluxina`, both `work/meluxina/bootstrap`.
Exact baseline commits and evidence live in fleet `notes/meluxina.md`.
Catpath's [DFT/Slurm proposal](https://github.com/retospect/catpath/blob/9a4cfede3efaa4f2d8aa2afb92427db41e4a8096/docs/proposals/dft-refinement-and-slurm.md)
owns broader task-runner exploration; link/update it when implementing this
slice, without another competing integration spec. Its GPU-job packing and
persistent-spool suggestions are proposals, not implemented MeluXina behavior.
The repository reference pins reviewed documentation, not a production engine
selection; it works independently of a sibling worktree's session layout.

## Scope / holds

First slice: opt-in remote manifest, immutable locally built catpath wheel,
stage/submit/recover/cancel/collect, one independent real ML energy/forces
fixture. Parallel seeds use the same lifecycle; pipeline-wide relax/NEB
scheduling, persistent spools and DFT follow later. LLM plan is separate.

No Precis `uv.lock` change or production catpath release selection; assigned
catpath SHA is a development baseline; the 0.23.1 recommendation remains
unanswered. Resources, budget, fixture/model and tolerances remain proposals.
No qu164903 tick, held campaign rerun,
legacy backfill, hydride NEB, MP-key experiment, push/merge/deploy, Docker prune
or full suite without coordinator scheduling. Never edit either main checkout.

## Reusable library boundary

Reto's reusable Slurm-library direction supersedes catpath-owned transport.
Generic contracts: `stage(bundle, hashes)`, `submit(job_spec, token)`,
`status(handle)`, `recover(intent)`, `cancel(handle)`, `collect(handle, outputs)`.
Bundles, resource profiles, submission intents, scheduler IDs and outcome
envelopes contain no ML/Python-engine assumptions. Persistent handles contain
profile IDs and hashes; no agent sockets or credential bytes.

Catpath maps energy/forces and seed tasks into bundles, executes its wheel/model
worker and validates scientific output. Precis resolves vault references,
supplies authenticated transport, reserves shared budgets, persists handles
and records every outcome. Generic transport/scheduler code imports neither
Precis Store/vault nor catpath/model code. Credentials are never resolved remotely.

Smallest initial location proposed: `src/precis/remote/`, with neutral `ssh.py`
and later `slurm.py` behind this contract; `credentials.py` is the explicitly
Precis-dependent bridge. Catpath adapter consumes an injected transport/runner,
not a Precis dependency. Repository/package extraction, library naming/version,
distribution/publication and dependency wiring remain proposals for separate
review. No top-level dependency, optional extra or publication is authorized.

## Verified seams / missing consumer

| Code anchor | Evidence / consequence |
|---|---|
| Catpath `pipeline.py::run` / `run_one_seed` | Serial model/seed loop; one seed runs a whole network. No implemented SSH/Slurm runner or task worker under `src/autocatpath`. |
| Catpath `calculators.py::make_calculator` / `_load` | Lazy ASE seam; process cache; explicit model/device/dtype/cueq. Tiny worker can evaluate energy/forces without a network. |
| Precis `src/precis_pathway/seed_job.py::_submit/_poll/_kill` | Existing per-model/seed jobs run local detached processes; preserve `meta.partial` and aggregate shape. |
| `src/precis_pathway/runner.py::submit_seed_partial_detached`, `types.py::DetachedHandle` | Local PID/PGID/scratch handle; cannot represent remote scheduler identity. |
| `src/precis/workers/executors/ssh_node.py::_run_one` / `run_ssh_node_pass` | Detached protocol, committed intent, restart adoption; unknown acknowledgement fails without reconciliation. Executor name supplies no SSH transport. |
| `src/precis/secrets.py::get_secret` / `set_secret` | Env → audited bound vault → file fallback. Generic resolution does not prove vault origin. |
| `src/precis_web/routes/secrets.py::set_secret` | Forwards value without stripping; blank write is no-op. |
| `src/precis_web/templates/secrets/index.html.j2` | Add/replacement values use single-line password inputs; no multiline key editor. |
| `src/precis_web/secret_status.py::KNOWN_SECRETS` | No SSH/MeluXina validator or passphrase pairing. |
| `deploy/roles/ssh_tunnels` | Key paths, not vault values; external keyscan preseed does not independently establish trust. |

No supported vault-to-SSH key/passphrase consumer found in Precis or catpath.
Supplied secret reference was not revealed; encoding/encryption/usability are
unknown. No raw-key workaround, authentication or `myquota` attempted.
Exact missing seam: audited vault-origin resolution → encrypted-key unlock →
bounded SSH transport with independently pinned identity and deterministic
credential teardown. Login confirmation does not prove this path.

## Credential / host contract — Precis

Private runtime profile: endpoint/port/user, project paths, opaque credential
references, verified host-key set. Never commit user-specific connection values
or private material. Supplied reference stays in private assignment; do not
ask registration/access questions again.

Implement a versioned credential contract, inspecting only redacted metadata
before choosing encoding; encrypted OpenSSH key and explicitly paired
passphrase references. Never silently treat an arbitrary string as JSON,
a key path or a shell command. Require vault origin for this pilot, rejecting
ambient env/file override so successful SSH proves the requested web-vault path.

Add write-only multiline key/file input; keep passphrase masked/separate,
preserve embedded newlines (normalize CRLF only), blank writes as no-op,
no existing secret in HTML. Validation returns presence/format/unlock/auth
classifications only. Synthetic-key tests cover multiline replacement,
encrypted unlock, missing/wrong passphrase and redaction.

Chosen facilities for the proposed credential patch: Python stdlib and system
OpenSSH (`ssh`, `ssh-agent`, `ssh-add`); no new Python dependency, crypto
implementation, package install or change to dependency locks. Commands are
available on the inspected host (OpenSSH 10.2p1); deployed worker availability
and askpass behavior must pass synthetic checks before use.

Strict host verification against independently verified pins; missing/changed
key fails before auth. Balanced endpoints need an allowed verified key set
and explicit rotation. Keyscan discovers candidates, not trust. No forwarding,
ambient identity or first-use acceptance; bounded timeouts, no prompts.
Actual host pin/provenance is still missing.

## Smallest reviewable credential patch — proposed, not implemented

One Precis-only patch, no scheduler/job/quest wiring or catpath changes.
No automatic live probe, quota request or SSH entry in the web's periodic
`secret_status` probe registry. Constructing transport does not connect.

| Module | Exact bounded change / contract |
|---|---|
| `src/precis_web/templates/secrets/index.html.j2` | Add explicit Multiline toggle on existing add/replacement forms; render one enabled `value` control (password or blank textarea). Multiline editor holds only newly entered text; no reveal/repopulation, file upload or new endpoint. Passphrase stays single-line/masked. |
| `src/precis_web/routes/secrets.py::set_secret` | Existing POST already preserves string value and blank no-op; retain behavior, update docstring/tests only. Normalize CRLF in key consumer, not every vault value. |
| `src/precis/secrets.py` | Add `require_vault_secret(name, *, store)` bypassing env/file/cache. Reuse audited `_reveal` with new opt-in redacted-error flag: no raw exception text or traceback; missing/error fails closed with `VaultSecretUnavailable`. Existing `get_secret` order and migration overload fallback unchanged. |
| New `src/precis/remote/credentials.py` | `vault_ssh_session(store, credential_refs, profile, *, scratch_root)` context manager resolves explicit key/passphrase refs, unlocks dedicated agent, yields generic `SshSession`, then tears down. Secret-bearing fields excluded from repr; no serializable credential result. |
| New `src/precis/remote/ssh.py` | Neutral immutable `SshProfile(host, port, user, verified_host_keys, pin_provenance)` plus `SshSession.run(remote_argv, *, timeout_s)`. Fixed OpenSSH options, local argv list, quoted remote argv, bounded process-group execution. Receives agent/public-identity paths; imports no vault/Store/catpath. Output is internal, repr-hidden, never logged by transport. |
| New `src/precis/remote/_askpass.py` | Internal helper with no vault access; reads one passphrase from private Unix socket and writes only to OpenSSH's askpass pipe. No CLI registration, terminal prompting, prompt echo or logs. |
| New `src/precis/remote/__init__.py`, existing `src/precis_web/__init__.py` | Owning contract/rationale: neutral core vs vault bridge, short-lived OpenSSH identity and write-only multiline input. |

Private config v1 explicitly maps `key_ref` to encrypted OpenSSH text and
`passphrase_ref` to passphrase; supplied ref's existing encoding remains
unknown. Unsupported format yields `credential_format`, never reinterpretation
or rewrite. Bounded stdlib base64/length-framing check of
[OpenSSH key envelope](https://raw.githubusercontent.com/openssh/openssh-portable/master/PROTOCOL.key)
rejects unencrypted `cipher=none`/`kdf=none`, NULs, oversized/multiple keys and
non-Ed25519 public identity. OpenSSH performs decryption/validation. Missing
passphrase fails before agent start; wrong passphrase returns `unlock_failed`.

Session sequence:

1. Validate explicit profile/pins and consumer-owned scratch root; no keyscan,
   inherited SSH config, wildcard pins or guessed passphrase reference.
2. Resolve vault-only values with explicit Store. No generic TTL cache; no
   values in argv, child env, exception/cause, manifest or log.
3. Create mode-0700 session directory under explicit worktree/session scratch,
   mode-0600 encrypted-key file and pinned known-hosts file; no unlocked key
   or passphrase file. Prevent symlink/path escape; Unix socket length checked.
4. Start dedicated `ssh-agent -D -a <socket>` with stdlib `Popen`, sanitized
   env and owned process group. No ambient agent, shell/eval or debug output.
5. One-shot bounded passphrase broker on private mode-0600 Unix socket in that
   directory; helper path/socket location in env contain no secret. Fixed helper
   launcher references installed module/current interpreter only. Force
   `SSH_ASKPASS_REQUIRE=force`; `ssh-add -q -t 60 <encrypted-key>` reads helper
   output internally, never through captured user logs. Deny repeated requests;
   synthetic TTL test uses a shorter lifetime bounded by the same 60-second cap.
6. Obtain public identity from dedicated agent internally; use its public file
   for `IdentityFile` + `IdentitiesOnly=yes`; delete encrypted-key file after
   successful load. Yield session with sanitized subprocess env only.
7. All exits close broker/FDs, terminate/reap only owned children, unlink session
   files/socket/dir. Failure during any step runs same cleanup; do not replace
   primary error with cleanup error. Hard controller kill is not a finally path:
   agent identity expires within 60 seconds; encrypted-only residual scratch
   may persist. Each session holds a non-inherited advisory lock; next startup
   reclaims only same-owner, nonsymlink session dirs whose lock can be acquired.
   Never kill a PID from stale metadata without identity verification.

Per command use `ssh -F /dev/null`, own `IdentityAgent`/public `IdentityFile`,
`StrictHostKeyChecking=yes`, own `UserKnownHostsFile`, global known-hosts off,
`UpdateHostKeys=no`, `VerifyHostKeyDNS=no`, `BatchMode=yes`, public-key-only,
`ForwardAgent=no`, `ControlMaster=no`, `ControlPersist=no`, no proxy/forwarding,
one connection attempt and bounded connect/process/output limits. Reject
option-like/control-character hosts/users and untrusted command interpolation;
quote remote arguments with `shlex.join`. Expired identity requires a new
bounded vault session, never silently a permanent agent.

Allowlisted diagnostics only: credential unavailable/format/passphrase missing,
unlock failed, pin missing/mismatch, auth failed, transport timeout/unavailable,
cleanup failure. The Precis bridge exposes no raw OpenSSH/key/DB stderr.
Generic result bytes stay internal until a workload-specific validator chooses
safe outputs. Same-UID processes are already within the vault trust boundary;
Unix sockets and finite identity lifetime do not create a per-user vault ACL.
[ssh-add](https://man.openbsd.org/ssh-add),
[ssh-agent](https://man.openbsd.org/ssh-agent),
[ssh_config](https://man.openbsd.org/ssh_config) support this facility choice.

Focused acceptance for this proposed patch:

- Extend `tests/precis_web/test_secrets_route.py` / `test_secrets.py`: multiline
  add/replace preserves embedded newlines, one enabled value control, blank
  no-op, stored text absent from returned HTML. Render without vault reveal.
- Extend `tests/test_secrets_resolver.py` / `test_secrets_access_audit.py`:
  conflicting env/file/cache cannot win; missing/broken vault fails closed;
  audit identity and existing overload fallback persist; sentinel exception
  text/credentials absent from logs and raised error/cause.
- New `tests/test_remote_ssh_credentials.py`: synthetic encrypted Ed25519 key
  under fixture scratch; real offline `ssh-agent`/`ssh-add` unlock (no network),
  wrong/missing passphrase, CRLF, unencrypted/malformed key, process/env/argv
  redaction, file modes, all timeout/failure/exception cleanup branches, finite
  agent TTL after simulated controller death. No user credential access.
- New `tests/test_remote_ssh.py`: config isolation, pins required, argv quoting,
  bounded output/timeouts and child reaping; transient loopback-only synthetic
  `sshd` tests correct pin acceptance and changed/missing pin rejection. If test
  runtime lacks required binaries, report gate unavailable, not mock success.

Proposed focused command: `scripts/test -n0 tests/test_secrets_resolver.py
tests/test_secrets_access_audit.py tests/precis_web/test_secrets_route.py
tests/precis_web/test_secrets.py tests/test_remote_ssh_credentials.py
tests/test_remote_ssh.py` with scratch/tmp configuration confined to worktree
or session scratch. Ruff and targeted container typecheck follow; full suite
requires coordinator scheduling. No tests or source patch run in this follow-up.
Patch authorization covers synthetic verification only; live credential reveal,
authentication and quota remain a later explicit gate.

## Artifact / task contract — catpath

Build once with `uv build --wheel` from reviewed exact catpath commit in its
assigned worktree; record clean/dirty state and patch hash. Source SHA,
distribution version and wheel SHA256 are mandatory; a different hash is a
different artifact even at the same version. Never choose latest/branch head
or the assigned development baseline by default.

Separate Linux x86_64 dependency lock/hash manifest; leave Precis `uv.lock`
alone. Pin Python/ABI/glibc/wheel tags, torch/CUDA/MACE/ASE/numpy, module stack
or container digest, model revision/license/weight SHA256. Mac wheels and
Spark/Blackwell settings do not establish A100 compatibility. One ML backend
per environment. Verify imports/dependency consistency/CUDA in allocation.

Stage hashed Linux wheelhouse/model once, verify transfer bytes; install the
local wheel by path in an isolated environment with offline hash-checked
requirements. Record installed artifact/packages. No per-task model downloads.
If Apptainer is needed, build elsewhere, pin digest; no login-node image build
or assumption an unpublished image exists.

Manifest v1: run/task/attempt IDs, kind, canonical input/config hashes, extxyz,
backend/model/dtype, source/wheel/dependency/weight hashes, expected outputs,
timeouts/resource profile. Task IDs include artifact hashes, not just version.
Initial kinds: `energy_forces`, seed adapter to `pipeline.run_one_seed`;
fine-grained NEB/DFT deferred. Backend operations: stage, submit, recover/status,
cancel, collect; local execution shares the task/output contract.

Proposed Precis `autocatpath_remote` plugin job uses existing `JobTypeSpec`
submit/poll/kill + `ssh_node` for pilot; seed job gains explicit remote opt-in
later, preserving current local defaults/partial shape. Existing metadata
and plugin entry points; no new MCP verb or schema migration.

## Lifecycle / recovery

| Operation | Required behavior |
|---|---|
| Stage | Profile-controlled project scratch/run/hash path; bounded transfer, restrictive permissions, hash verification, atomic ready marker; no credentials/DSN remotely. |
| Submit | Commit remote intent handle before network: backend/profile ID, run/token, manifest hash, path, phase, time, eventual job/array/step IDs. Remote per-token lock; `sbatch --parsable`, explicit resources/export allowlist; atomic shared-storage receipt. |
| Recover | Re-adopt intent/receipt; reconcile `squeue`, time-bounded `sacct`, unique job name/comment token and manifest hash. One match adopts; conflicts stop; uncertain zero-match remains unresolved, never blind resubmit. Queue disappearance is not completion. |
| Cancel | Validate run/user/account/token and job/array/step IDs before `scancel`; observe terminal state, collect available outputs. Record every cancelled/never-started task. Transport failure means cancel pending, not confirmed. |
| Collect | Retry by checksum; validate envelope/schema/task/input identity, units and outputs. Persist outcomes/logs before cleanup; collection repeats are idempotent. COMPLETED with missing/corrupt output fails. |

Remote intent handle keeps existing unknown-submit guard from discarding the
reconciliation path: persist before submission, retain/return it after uncertain
acknowledgement, poll by phase. Controller death must not cause a second
allocation. Test crashes before submit, after acceptance and before receipt/DB
acknowledgement. Lost lock plus missing receipt is still uncertain; no auto retry.

Proposal: queue timeout 30 min, compute walltime 10 min, collection/accounting
grace 5 min. Current executor deadline starts at submit: use bounded end-to-end
budget, not just compute time. On expiry preserve intent and reconcile cancel/
accounting to durable outcome even after job row becomes terminal.

EVERY submitted task gets an outcome (controller-synthesized if output absent):
IDs/hashes/attempt, timestamps, scheduler state, exit/signal, classification
(succeeded/failed/timeout/cancelled/missing_output/invalid_output/submission_unknown),
error, log/artifact hashes and allocation usage. Unknown remains explicit
unresolved evidence, never success/omission; finalize when evidence arrives.
Failed tasks stay in aggregate coverage; surviving seeds are not a complete run.

## Provider / bounded parallelism

Official references checked for this design:

- [Connecting](https://docs.lxp.lu/first-steps/connecting/): key/passphrase auth.
- [Usage policy](https://docs.lxp.lu/access/PoliciesSummary/): login nodes for
  submission/file work; ML and long-lived workers on compute nodes.
- [Allocations](https://docs.lxp.lu/access/allocation_monitoring/#myquota):
  after supported consumer, one auth check plus one `myquota`; shared quota
  gate ≥10 seconds, no watch. Record project/storage/node-hours, then permitted
  QoS; visible partition or documentation example is not entitlement.
- [Job guide](https://docs.lxp.lu/first-steps/handling_jobs/): full-node exclusive
  allocation even with fewer cores. `srun` task steps; arrays support `%K` cap.
- [Architecture](https://docs.lxp.lu/system/detailed_arch/): four A100 40-GB GPUs
  per GPU node. Do not assume separate one-GPU jobs pack across allocations.
- [sbatch](https://slurm.schedmd.com/sbatch.html) /
  [sacct](https://slurm.schedmd.com/sacct.html): parsable ID/accounting contracts.

Pilot proposal: one exclusive node, gpu partition, test QoS only if allowed,
actual account, one task, four CPUs, one GPU task step, 16-GiB memory request,
ten-minute Slurm ceiling. Full-node reservation still applies; cap exposure
at 1/6 node-hour. Different entitlement/stack profile returns for review first.

Later parallel slice: up to four independent `srun` workers within one
allocation, one per GPU; manifest cap/device availability, separate envelopes,
GPU/CPU binding/thread limits. First parallel acceptance uses two distinct
inputs and proves overlap. Homogeneous arrays across nodes start capped at one
allocation; array concurrency is not merely a GPU count.

Precis shared controller limits: one pending/running allocation initially,
submission ≥10 seconds apart; batched status ≥30 seconds, jitter/backoff to
five minutes; bounded attempts/bytes/node-hours. All workers share limits;
account/QoS remains authoritative. Persist reservation before submit, count
unknown acknowledgements against in-flight cap.

## First real pilot / acceptance

Independent four-atom periodic Cu fcc conventional cell, a=3.61 Å; displace
one atom +0.02 Å along x. Pin extxyz bytes/hash. No constraints, relax or
reaction network. Propose MACE-MP-0 small pretrained checkpoint; freeze actual
revision/license/weight hash before run. No MP API key. Through catpath seam:
`MLIPConfig(backend='mace', model=<staged checkpoint>, device='cuda',
dtype='float64', cueq='off')`; scalar energy eV, (4,3) forces eV/Å, actual
device/model hash. [MACE reference](https://mace-docs.readthedocs.io/en/latest/guide/foundation_models.html)
supports pretrained ASE calculators; verify local-checkpoint support against
the exact dependency revision before freezing artifacts.

Before submission fix CPU reference values on an appropriate compute host with
same wheel/model/input/dependencies. Freeze absolute tolerances: energy ≤1e-4 eV,
max force-component difference ≤1e-4 eV/Å; all finite, exact shape/units.
Mismatch fails acceptance; do not widen tolerances after seeing results.
This validates infrastructure, not DFT accuracy.

Live gate: actual job/step IDs, account/QoS/resources, all hashes, successful
compute/scheduler exit, collected checksums, CPU comparison, elapsed/AllocTRES/
node-hours, Precis job/outcome handle. Disconnect/recover without duplicate
submit, collect twice without duplicate graph outcomes. SSH login, EMT and
mock scheduler are insufficient. No real job has run in this checkpoint.

Implementation checks via focused `scripts/test`: state transitions, uncertain
submit recovery, cancel/timeout/accounting delay, partial/missing output,
artifact tamper, per-task failure coverage, credential redaction and shared
concurrency. Catpath worker/backend tests in its worktree. Ruff/types plus
coordinator-scheduled full ship gate follow; syntax/mock success is not live gate.

## Separate later LLM plan

Batch qualification after ML gate: graph-memory workload leads; paper/catalysis
receive equal research allocation. Pin small graph eval pack, reference/scoring,
model revision/license/weights, context/quantization/runtime and measured A100
fit. Compare completion coverage, quality, queue/startup, throughput/node-hours;
record every prompt outcome. Model/resource selection follows entitlement;
model size does not establish frontier quality.

`llm_catalog.py::seed_slullama_card`, static-source prune guard and
`local_serving.acquire` slot accounting shipped; keep dark. Earlier slullama
login-node proxy/idle daemon proposal deferred because policy excludes long
login processes. Batch pilot enables no rung, always-on allocation or interactive
serving; any later service needs permitted endpoint lifecycle/routing review.
`router.py::_skip_unserved_local_rung` only handles `Transport.LOCAL`, not
`OPENAI_TOOLS`: loopback proposal does not prove clean non-serving-host fallback.
Static card seeding replaces full `served_by`: distinct model ID if revisited.

## Coordinator review / next gate

First seam/spec deliverable accepted by parent; this amendment awaits review.
Review reusable core/vault/workload boundary and proposed credential-only patch;
authorize that bounded source patch separately. Resource/budget/tolerance and
0.23.1 engine recommendation remain unanswered proposals. Then review live
credential access before validating pinned vault-backed auth and one quota
query, freeze actual account/QoS/environment/model/artifact hashes and CPU
reference, review concrete run manifest before one submission. GPAW/DFT is
not prerequisite; production release/held campaigns/LLM routing stay separate.
