---
status: draft
pillar: local-compute
---

# MeluXina — vault-backed catpath SSH/Slurm; later LLM placement

Coordinator accepted spec and credential plan; implementation plus one bounded
real ML pilot now authorized. Source/synthetic security review precedes actual
secret use; afterward authentication, one quota query and one <=10-min/one-node
fixture within allocation and <=25USD limit need no further user permission.
Pinned vault-backed authentication and ONE quota query were verified2026-10-05;
p200916 has50 monthly GPU node-hours remaining in that accepted snapshot.
Compute runtime, complete storage/artifact closure and live ML success remain
unverified. Canonical integration spec consolidating
communicator's draft and the chemistry/local-compute ownership boundary.

Owner: meluxina; Precis orchestrates credentials/jobs/graph outcomes; a generic
SSH/Slurm layer owns remote lifecycle; catpath is its first workload adapter.
Implementation worktrees:
`codex-meluxina`, `codex-catpath-meluxina`, both `work/meluxina/bootstrap`.
Exact baseline commits and evidence live in fleet `notes/meluxina.md`.
Chemistry13 exclusively owns `codex-catpath-chemistry` on
`work/chemistry/meluxina-pilot`. Its earlier973491d4 base assignment is
superseded: chemistry reviews reapply of additive worker/adapter patches onto
selected local0.24.0 source9a4cfede, supplies a newly built immutable wheel and
fixture/artifact hashes; branch/patches retained, no production pin change. Original `codex-catpath-meluxina` stays
read-only. Meluxina owns the sole live submission; production engine/lock
and frozen149db0357 remain unchanged.
Catpath's [DFT/Slurm proposal](https://github.com/retospect/catpath/blob/9a4cfede3efaa4f2d8aa2afb92427db41e4a8096/docs/proposals/dft-refinement-and-slurm.md)
owns broader task-runner exploration; link/update it when implementing this
slice, without another competing integration spec. Its GPU-job packing and
persistent-spool suggestions are proposals, not implemented MeluXina behavior.
The repository reference pins reviewed documentation, not a production engine
selection; it works independently of a sibling worktree's session layout.

## Scope / holds

R13 bounded completion: generic Precis bootstrap owns deterministic expected
runtime/resource/path checks; chemistry owns its structured early-safe preflight
report producer/adapter and all 65 pinned dependency archives. No duplicate
report schema or domain validation. Render/bootstrap bytes are hash-bound in the
final wheelhouse inventory. A fixed baseline system Python can run metadata
checks before the target venv exists; exact target Python patch/path/hash,
Linux/x86_64/glibc>=2.28, compatible CUDA13 driver, hash-bound Linux uv, allocation
identity/resource/time and selected private path/headroom checks fail closed
inside the SAME sole allocation, before install/model. No second preflight job,
automatic fallback, module default, dependency resolution or login-node install.
System-Python/no-modules is an explicit expected policy, not an assertion about
compute-node availability; a pinned uv runtime artifact adds no package dependency.
Chemistry's early-safe report CLI remains its owning contract. Generic bootstrap
exports bounded neutral facts and fails closed; chemistry's thin wrapper maps
these to its aliases/intermediate report without raw command stderr. Hash both
the generic component and final workload wrapper, never assume their contracts
are interchangeable.

Proposed concrete sole vector: p200916/gpu/test, one exclusive node, one task,
128 CPUs, 4 GPUs,491520MiB and explicit600s; freeze only after parent/higher
association and covered-allocation/cost evidence. Existing runner flags and
<=3GiB payload override stay unchanged. Stage/run/env/temp/output/venv/generated
metadata/dirs/symlinks/pyc counts and peak bytes require complete chemistry
inventory plus shared-project reserve against the accepted26664-file headroom;
no SCRATCH or other-user/home environment adoption. Final packet records every
unknown separately, exact command/immutable identities and source review status;
source review precedes merge/stage. R13's verified65-wheel inventory measures
24408 expanded files plus2307 directory upper bound; conservative shared-project
peak80102 inodes plus1024 reserve exceeds accepted26664 free files. The existing
generic runner uses one remote root for stage and run. No separate home/SCRATCH
scope or root split is selected automatically: admitted storage and any narrowly
reviewed path contract are prerequisites for a runnable freeze. This is authorisation to finish R13 source
and factual gates, not to submit while any gate is missing.

R13 candidate identities (login observations, not compute attestation): baseline
`/usr/bin/python3`3.6.8 SHA256
`6f8a05e5f9a6eb002ea9561c313ed1e00d11e84a9bcfde56af92ccf21dee9c4c`;
target `/usr/bin/python3.12`3.12.14 SHA256
`8fd63eaafaf82d27382341500cefe715cb94098a01627fbb3540cbdb056a9fa5`.
Launchers do not hash-attest the whole OS or dynamic libraries. Staged Linux
x86_64 uv0.12.22 executable47991416 bytes SHA256
`96e1603cb62aebb1a804fe9866a5708ee8bba4c39d07202ad29cf256a67366c3`
was extracted from the size/hash-verified official PyPI wheel; never executed
locally or installed. Pin source URL/archive hash in the final artifact record.
The private runtime copy and temp directory consume additional bytes/inodes.
Require authoritative `confstr` glibc>=2.28 and CUDA13 driver>=580.65.06 on the
actual allocated A100 node, exact hashes/versions and available `venv`; no default
modules. UTC scheduler start/end and actual allocation owner/node/vector/time are
parsed rather than filled from expectations; missing early-report primitives fail.

Association cache now establishes u10418617621→p20091610543→luxembourg5→root1;
no tighter observed inherited per-job wall/node/TRES fields. p200916's GPU-node
minute limit3057 with0 used covers10 minutes; own MaxSubmit100/current0 is distinct
from aggregate parent counts. Proposed covered-allocation lane consumes at most
1/6 of the accepted50 GPU node-hours, with no purchase action. Native quota/limits
do not reveal a USD tariff or contract billing: do not fabricate a zero-dollar
rate. A paid-metered lane would require verified rate<=150USD/node-hour to remain
within25USD for600s; coordinator reviews the covered-allocation interpretation.

First slice: opt-in remote manifest, immutable locally built catpath wheel,
stage/submit/recover/cancel/collect, one independent real ML energy/forces
fixture. Parallel seeds use the same lifecycle; pipeline-wide relax/NEB
scheduling, persistent spools and DFT follow later. LLM plan is separate.

No Precis `uv.lock` change or production catpath release selection; assigned
catpath SHA is a development baseline; Reto selected local catpath0.24.0 source
`9a4cfede3efaa4f2d8aa2afb92427db41e4a8096`; chemistry must review reapply of
its earlier973-base additive patches and build/hash a NEW wheel. Stale dist0.22
and prior0.23.1 recommendation are superseded. Production pin remains unchanged. Account/resources/model artifacts must be frozen from entitlement and chemistry evidence.
The authorized hard cap is one node/600 seconds/25USD; proposed scientific
tolerances remain frozen before submission, never widened from observed results.
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

## Verified initial seams / implemented consumer checkpoint

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

At accepted planning baseline, no vault-to-SSH consumer existed. The bounded
implementation now adds the consumer below; actual credential usability remains
unverified until source/security review passes.
Initially the supplied reference was not revealed; the missing seam was audited
vault-origin resolution → key unlock → pinned transport and teardown. Reviewed
source now supplies that seam. Private read-only diagnosis found canonical
OpenSSH BEGIN/END markers but no actual line breaks or literal newline escapes:
`multiline_framing_missing`, before algorithm/encryption or agent validation.
No private content was emitted, rewritten or converted; no SSH or `myquota`
attempted. Reto must restore original multiline text to the same vault entry
through a newline-preserving writer. R10 version8.35.9/source516dc91de45b9616990be475458bc5785b4be4b5
deployed and runtime-verified the reviewed multiline web writer. This resolves
the writer-integration dependency, not stored-key restoration or pilot success.
The latest private check identified unencrypted Ed25519 metadata but still no
line breaks; cryptographic usability/authentication remain unverified.

## Credential / host contract — Precis

Private runtime profile: endpoint/port/user, project paths, opaque credential
references, verified host-key set. Never commit user-specific connection values
or private material. Supplied reference stays in private assignment; do not
ask registration/access questions again.

Implement a versioned credential contract, inspecting only redacted metadata
before choosing encoding; encrypted OpenSSH key and explicitly paired
passphrase references when explicitly declared encrypted. Reto now declares the
existing vault key unencrypted with no passphrase and explicit username; no
paired-reference requirement for that case. A missing reference does not silently
change an encrypted contract: consumer mode is explicit. Never silently treat an arbitrary string as JSON,
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
Provider HTTPS connecting documentation publishes an Ed25519 key and
SHA256 fingerprint; coordinator independently verified the fingerprint. Use
that exact public identity from the private runtime profile, fail closed on
rotation; no keyscan trust. Reto explicitly declares no passphrase; any mismatch of actual key envelope
with that declaration fails format validation, without guessing a reference.

## No-passphrase correction — internal review before real values

After verified Precis deployed46591fa40b3c, Reto resumed the existing bounded
pilot. User declares the existing key multiline and unencrypted, without a
passphrase; remembered PGP header is uncertain, not a verified key format.
No new secret name or conversion. Validate privately through the strict
vault-only consumer after source/synthetic internal review; unsupported PGP/
PEM/SSH format fails with credential_format and no key content.

Smallest patch: CredentialRefs gets explicit encrypted/unencrypted declaration;
encrypted mode preserves paired ref, aes256-ctr/bcrypt Ed25519 and one-shot
askpass. Unencrypted mode requires no passphrase ref, supports only validated
OpenSSH Ed25519 none/none envelope, feeds key to `ssh-add -k -t60 -` over a
bounded nonblocking stdin pipe, never a plaintext scratch file/argv/env.
OpenSSH validates crypto and owns the dedicated finite-TTL agent. No prompts,
no unencrypted file surviving hard kill; only public identity/pins/lease on disk.
PGP is never converted; unsupported algorithms/envelopes remain an explicit
format blocker, not a passphrase question. Synthetic tests prove real unencrypted
agent load/TTL/loopback pinned auth, no plaintext file, no passphrase lookup,
encrypted-mode mismatch rejection, output redaction and cleanup/timeouts.
Review new source SHA before any actual vault value. Prior review never
implicitly covers this source change. No production dependency/lock selection.

## Accepted credential patch — source/security checkpoint required

Precis owns credential/core patch; chemistry owns adapter changes separately.
No scheduler or job hot path triggers authentication automatically.
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

Private config v1 explicitly maps `key_ref` to OpenSSH text and `encryption`
to the caller's declared encrypted/unencrypted contract. `passphrase_ref` is
mandatory only for encrypted mode and forbidden for unencrypted mode. Actual
supplied key format remains unknown until privately validated after review. Unsupported format yields `credential_format`, never reinterpretation
or rewrite. Bounded stdlib base64/length-framing check of
[OpenSSH key envelope](https://raw.githubusercontent.com/openssh/openssh-portable/master/PROTOCOL.key)
requires aes256-ctr/bcrypt in encrypted mode or none/none with empty options in
unencrypted mode; rejects NULs, oversized/multiple keys and non-Ed25519 identity. OpenSSH performs cryptographic validation. Missing encrypted-mode passphrase
fails before agent start; wrong passphrase returns `unlock_failed`. Unencrypted
mode resolves no passphrase and uses no broker/key file, only bounded stdin.

Encrypted session sequence (unencrypted omits key file/askpass steps and loads
validated key via bounded stdin with SSH_ASKPASS_REQUIRE=never):

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
6. Extract public identity from the bounded encrypted envelope; OpenSSH verifies
   its consistency during unlock. Use its public file
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
requires coordinator scheduling. Synthetic source checkpoint now exists; exact commit and retained test logs
are reported in fleet inbox, with no live credentials or remote job used.
Before secret use, submit source plus synthetic results for internal review.
After review passes, the assigned vault-backed live auth/quota/pilot steps are
authorized; missing refs/passphrase/pins or fixture must be reported, not guessed.

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

## Reviewed staging seam correction — explicit profile, no cap bypass

Chemistry sourcef22165a prepares newly built pristine0.24 and separately labeled
0.24+meluxina.pilot1 wheels. Generic `_name` must allow literal `+` in safe
relative artifact names; preserve traversal/absolute/option/control/shell-token
rejection. Transfer and hash commands remain argv/quoted positional arguments.
No alias/relabeling of the pilot distribution as a pristine release.

Pinned candidate dependency bytes2,960,292,950 plus model32,581,838 total
2,992,874,788 before wheel/input/manifest/runtime script. Generic2GiB default
stays unchanged. Proposed pilot profile explicitly supplies
`Limits(max_bundle_bytes=3 * 1024**3)` (3,221,225,472 bytes), leaving228,350,684
bytes for bounded reviewed extras. This is a cap on caller-supplied artifact
payload bytes, not total remote storage: generated `ready.json`, filesystem
metadata, extracted dependencies/environments, temporary files and outputs
are outside that counter and require a separate explicit storage/quota budget.
Review/freeze
actual inventory/runtime/bootstrap hash and exact total <=profile limit; no
expensive downloads or actual staging before supported auth/discovery. Profile
storage budget must also fit actual allocation/quota, independent of compute cap.

Ready marker and local stage manifest persist actual bundle byte count and
configured byte limit; submitted durable intent carries both. This exposes any
chosen override in review/provenance rather than changing the default silently.
Synthetic tests use small bundles with low explicit limits to prove over-limit
rejection before remote calls and accepted overrides with recorded bounds;
no multi-GiB test buffers or fixture downloads. Separate internal narrow runner/
profile review precedes enabling that pilot configuration. Actual credential
format currently blocks live auth; staging fix does not bypass that gate.

## Lifecycle / recovery

Independent original-core review requires two bounded corrections before PASS:
stage preflights canonical distinct artifact paths, rejecting generated ready
and upload temporary-path collisions (including file/directory ancestors),
then verifies the complete inventory again before publishing readiness. Restaging
invalidates prior local/remote readiness before writes; no partial inventory is
advertised. Literal `+` names and reviewed explicit payload caps stay supported.

Collection failures persist constant classified evidence and an outcome for
every submitted task before propagating the error. Transport/checksum-command
failures remain collection-pending; corrupt or over-cap output is invalid-output
evidence. Preserve scheduler state/accounting, intent/job identity and prior
verified output hashes. Retrying `collect` on that same handle can resolve the
failure; it never submits again. Evidence stays bounded to latest error/attempt
counts and hashes, with no raw exception/transport text. The returned success
contract is unchanged; chemistry owns scientific/domain envelopes. Synthetic
regressions cover all three reported filename counterexamples, final-inventory
drift, transport/checksum/output-cap failure, durable all-task outcomes and retry.
OpenSSH connection-error status255 from output `cat` must enter that same
durable collection-pending path before missing-file handling; it proves no
artifact absence. Known absent-file command status retains missing-output behavior.

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

## Separate LLM batch placement plan — execution remains held

Batch qualification follows the real ML gate; no LLM allocation or weights
download in this assignment. Graph memory leads, with paper and catalysis
receiving equal research allocation. Proposed first frozen pack: 48 independent
prompts (24 graph relation/summary, 12 paper evidence extraction, 12 catalysis
evidence reasoning). Every prompt carries input Precis handles, source-content
hashes, expected schema/required evidence, reference answer and fixed scoring.
Missing evidence must produce explicit abstention; no unsupported citation.
Input handles are selected from authorized existing corpus, not invented here.

Candidate for review, not selection: [Qwen3-32B official model card](https://huggingface.co/Qwen/Qwen3-32B)
publishes Apache-2.0 licensing, 32.8B parameters, native 32,768-token context,
and a non-thinking switch. Proposed short graph lane uses non-thinking mode,
8,192-token input ceiling, 512-token output ceiling, deterministic documented
sampling and a pinned quantized artifact/runtime. Four-bit parameter arithmetic
gives roughly 16.4GB before quantization overhead, runtime and KV cache; this
is a fit estimate, not measured A100 compatibility. Require measured peak VRAM
within one 40GB GPU before accepting that placement. A pinned smaller candidate
is a later fallback decision if measured fit fails, never an automatic download.

Reuse neutral durable runner with one exclusive allocation at a time; one
initial GPU worker, then proposed up-to-four independent workers within the
same node only after quality/overlap evidence. Stage pinned tokenizer/template,
model revision/license/weight and quantization hashes, Linux dependency wheels
and batch input manifest offline; execute a finite batch process on compute
nodes, with no endpoint or login daemon. Account/QoS/time/memory and monetary
cap require separate approved run manifest; the ML pilot authorization does
not authorize this LLM batch execution.

Record every prompt outcome including invalid/truncated/timeout/missing,
completion coverage, schema validity, required-evidence recall, unsupported
claim/citation count, blinded quality against pinned reference, queue/startup
seconds, input/output tokens, throughput, peak VRAM, scheduler AllocTRES/runtime
and reserved node-hours per useful answer. Proposed qualification gate: full
outcome coverage, no unsupported citation, at least 95% schema validity and
no more than 5 percentage-point loss on pinned reference evidence recall.
Thresholds and reference scoring are reviewed before execution; runtime speed
alone cannot promote a model or route. Preserve graph lead and equal research
shares in scheduling/reporting, including failed work.

`llm_catalog.py::seed_slullama_card`, static-source prune guard and
`local_serving.acquire` slot accounting shipped; keep dark. Earlier slullama
login-node proxy/idle daemon proposal deferred because policy excludes long
login processes. Batch pilot enables no rung, always-on allocation or interactive
serving; any later service needs permitted endpoint lifecycle/routing review.
`router.py::_skip_unserved_local_rung` only handles `Transport.LOCAL`, not
`OPENAI_TOOLS`: loopback proposal does not prove clean non-serving-host fallback.
Static card seeding replaces full `served_by`: distinct model ID if revisited.

## Coordinator review / next gate

Accepted fcbd9b2ef seam/spec and c3654f098 credential plan now authorize source
implementation. Source plus synthetic security checkpoint requires internal
coordinator/architecture review before vault values. After that passes,
authentication, one quota read and permitted discovery are authorized. Freeze
actual entitlement, chemistry revision/wheel/model/dependency/input/script
hashes and CPU reference before sole <=600s/one-node/25USD submission. Persist
intent/IDs, disconnect/recover without duplicate allocation, collect all task
outcomes and capture actual scientific evidence through supported Precis verbs.
Unsupported actual key format or missing immutable fixture is a concrete blocker;
no paired passphrase reference is needed for the declared unencrypted key.
No additional user permission is required for already authorized steps.
GPAW/DFT and LLM execution/serving remain separate; local0.24/9a4 source selection requires a NEW chemistry wheel/reapply review;
production locks/frozen149db0357 stay unchanged.
