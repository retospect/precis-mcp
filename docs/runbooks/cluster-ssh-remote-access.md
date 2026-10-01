# Reaching the cluster from a remote controller

The laptop is often **not** on the cluster's LAN. Everything here is about
telling "unreachable because of how we asked" apart from "actually down" —
that distinction cost a wrong diagnosis on 2026-09-08 (two healthy machines
were reported dead and a power cycle recommended).

No literal addresses below: this repo is public and
`tests/test_deploy_tree_no_secrets.py` rejects tailnet/LAN addresses tree-wide.
Real coordinates live in the gitignored overlay `deploy/inventory/hosts.yml`.

## The shape

* The Macs and the two GPU nodes sit together on the cluster **LAN**.
* A remote controller reaches them over **Tailscale** only. The cluster LAN
  prefix is not routable from anywhere else, and that is not a fault.
* One Mac is on the cluster LAN and can be used as a **jump host** to give a
  LAN last hop.

## Bare `ssh <host>` works — no `-o IdentityAgent=none`

`ssh melchior` / `balthazar` / `caspar` / `castor` / `pollux` / `spark` just
works as user `deploy`. `~/.ssh/config` sets `IdentityAgent none` on the
cluster `Host` block, which forces ssh to read the passphraseless on-disk key
`~/.ssh/cluster` (ed25519) instead of the flaky macOS/1Password agent. The
reflexive `-o IdentityAgent=none` on every call is redundant; it stays valid
only as a manual override where the config block isn't loaded.

**ssh is first-value-wins per keyword.** A catch-all `Host *` (IdentityAgent →
1Password socket) placed *above* the cluster block matches first and its
IdentityAgent wins — the cluster block's `IdentityAgent none` becomes dead
code. Everything then routes through the 1Password agent, which is
unavailable at boot, so launchd autossh tunnels fail in a loop (`signing
failed for … "~/.ssh/cluster" from agent: communication with agent failed` →
`Permission denied`) until a human logs in and unlocks 1Password. Rule:
specific host blocks first, `Host *` last. Diagnose with
`ssh -G <host> | grep identityagent` — it must print `none`, not the 1Password
socket. Verify an override with `SSH_AUTH_SOCK= ssh -o IdentityAgent=none <host>`.

Boot-safe by construction: the controller's launchd autossh tunnels (one to
melchior, one forwarding caspar's pgbouncer and Postgres ports locally) use
the on-disk key, so they come up with no 1Password and no human. `balthazar`
has ssh but no tunnel plist.

**hermes.** `hermes` is a live macOS user on melchior: it runs the agent-lane
daemons and asa-bot, and isolates the Claude OAuth / `~/.claude` login from
the deploy fleet identity. Keep it. There is no `ssh hermes@melchior` block —
nothing automated ssh's in as hermes, and `deploy` has passwordless sudo.
Reach it with:

    ssh melchior 'sudo -u hermes -H bash -lc "<cmd>"'

**Ansible has its own `ssh_args`** (`ansible.cfg`) that bypass
`~/.ssh/config`, so it can still fail every host `UNREACHABLE` with
`sign_and_send_pubkey: signing failed ... agent refused operation`.
Two things do **not** work: `-e 'ansible_ssh_common_args=-o IdentityAgent=none'`
(crashes ansible 14 on py3.14 — its tty arg-parser chokes on `-o`), and merely
unsetting `SSH_AUTH_SOCK` (macOS ssh still honours `IdentityAgent` from the
config). What works is overriding `ANSIBLE_SSH_ARGS` wholesale, keeping the
config defaults and appending the flags (verified on a full redeploy,
`failed=0`):

    ANSIBLE_SSH_ARGS="-o ControlMaster=auto -o ControlPersist=60s \
      -o ServerAliveInterval=30 -o IdentityAgent=none -o IdentitiesOnly=yes" \
      ansible-playbook redeploy-precis.yml

Prod DB reads: `scripts/prod-psql "SELECT …"` wraps the hop
([`prod-db-access`](./prod-db-access.md)). Dumping a container's env
(`docker exec … printenv`) is blocked by the secret-filter classifier — infer
config from data instead.

## Never pin `HostName` to a LAN address

A `Host <node>` block that pins `HostName <lan-ip>` makes that node
**unreachable by name from anywhere off the LAN** — no ssh, no ping — while the
machine is perfectly healthy and reachable over the tailnet. It converts a
single-path outage into "the box is dead".

Leave `HostName` out. The bare name then resolves via MagicDNS, and Tailscale
still prefers a direct LAN path when one exists, so unpinning is strictly more
available and never slower by design. The node that had never been pinned was
reachable throughout the incident; the two pinned ones looked dead.

For **throughput** (copying models or datasets), add a second alias per node
that keeps the LAN address and jumps through the on-LAN Mac:

    Host <node>-lan
        HostName <lan-address-from-overlay>
        HostKeyAlias <node>
        ProxyJump <on-lan-mac>
        User deploy
        IdentityAgent none
        IdentitiesOnly yes
        IdentityFile ~/.ssh/cluster

(Earlier configs pinned `HostName` to a LAN address on `castor` and `pollux`;
removed 2026-09-08Z. They now resolve by name via MagicDNS exactly like `spark`,
which never had a pin.)

Use these only for bulk transfer. Latency is dominated by physical distance, so
they will not make a shell feel faster — what they buy is replacing a
bandwidth-limited relay leg with a LAN leg.

## Three traps that produce false "host is down"

1. **`tailscale status` showing `-` does NOT mean offline.** It means no active
   session. Three nodes showed `-` while up for 12 and 23 days. Confirm with
   `tailscale ping <node>` or ssh — never read `-` as down.
2. **ssh to a raw IP bypasses the `Host` block**, so `IdentityAgent none` never
   applies and you get
   `sign_and_send_pubkey: signing failed ... from agent: communication with
   agent failed` → `Too many authentication failures`. That reads like a host or
   auth failure but is only the 1Password agent. Use the **name**; if you must
   use an address, pass `-o IdentityAgent=none -o IdentitiesOnly=yes -i
   ~/.ssh/cluster`. The same applies to any **new** `Host` alias you add — it
   does not inherit the cluster block's agent settings.
3. **macOS has no `timeout(1)`.** A sweep built on it reports *every* host
   UNREACHABLE regardless of reality. Use ssh's own `ConnectTimeout`.

## The proper fix: advertise the LAN as a subnet route

The `-lan` aliases are a workaround. The real fix is a Tailscale subnet route
from the on-LAN Mac, after which the whole cluster LAN is reachable from any
tailnet device and the aliases become redundant:

    ssh <on-lan-mac> 'sudo tailscale set --advertise-routes=<lan-cidr>'
    # then APPROVE the route in the Tailscale admin console (web UI)
    tailscale set --accept-routes        # on the remote controller

Advertising is inert until approved, so the first step is safe on its own.
As of 2026-09-08 no routes are advertised. This also restores access to
LAN-only services (e.g. the llama.cpp endpoint) that the tailnet cannot see.

## Which commit is a host actually running

(Venv layout, convergence assert, version-vs-commit trap:
[`cluster-deploy`](./cluster-deploy.md).) Read the venv's `direct_url.json`, never a checkout's `HEAD`:

    ssh <node> "sh -c 'ls -d /opt/precis*/venv/lib/python*/site-packages/\
    precis_mcp-*.dist-info/direct_url.json | head -1'"

Run the probe under `sh` — the Macs' login shell is zsh, where an unmatched
glob is a fatal error rather than an empty expansion.

Do **not** trust `scripts/ship`'s deploy-lag footer as ground truth for the
fleet: it reports against a locally recorded marker. It was per-worktree until
2026-09-08 (`scripts/lib/deploy-state.sh` moved it to the shared git common
dir), and a stale marker over-reported the lag by an order of magnitude.
