# First-boot bringup of a new NVIDIA DGX Spark (GB10) cluster node

**When.** A new DGX Spark (GB10, 128 GB unified memory, Ubuntu 24.04 arm64)
joins the cluster. Done for `castor` and `pollux` (2026-08-05Z) — the paired
twins doing headless LLM + sim serving, linked hip-to-hip over the ConnectX
QSFP cable. An older standalone `spark` also exists. Serving stack:
[`spark-distributed-llm-serving`](./spark-distributed-llm-serving.md). Access:
[`cluster-ssh-remote-access`](./cluster-ssh-remote-access.md). Starved-userland
tell: pings but ssh banner-times-out (same class as the Spark compute-niceness
issue).

Addresses live in the gitignored overlay; none appear here.

## 1. Factory access

The image ships a login for the owner (uid 1000, in `sudo`) whose
`authorized_keys` already holds the cluster key, so
`ssh -i ~/.ssh/cluster <owner>@<node>` works out of the box (the cluster key —
despite the `.pub` name it is *not* the melchior key). That login's `sudo`
needs a password until the deploy account exists. "Too many authentication
failures" = the agent offered too many keys → add `-o IdentitiesOnly=yes -o
IdentityAgent=none -i ~/.ssh/cluster`.

## 2. Deploy account + hostname + apt

Canonical deploy account: uid **806**, gid **806** (gid == uid; the historical
1001 was retired 2026-08-08Z — `cluster_uid_gid_parity`), groups `deploy` +
`docker`, home `/home/deploy`, shell `/bin/bash`; `authorized_keys` =
`~/.ssh/cluster.pub`; `/etc/sudoers.d/deploy` =
`deploy ALL=(ALL) NOPASSWD: ALL` (chmod 440). The melchior-tunnel and
dev-user keys are appended later by the ansible `ssh_tunnels` role — don't
hand-add. The uid-806-outside-`UID_MIN` warning is cosmetic (intentional
system uid).

The bootstrap script (not in the repo; rebuild it) is **idempotent** and does:
deploy account + hostname + the apt de-wedge (§3) + `apt full-upgrade` +
`systemctl set-default multi-user.target` (headless) + the §5 hardening. Run
it once via `ssh -t <owner>@<node> 'sudo bash /tmp/bootstrap.sh'` (one
password prompt).

## 3. apt WILL be wedged out of the box

The factory image has third-party mozillateam-PPA (`~mt1`) firefox/thunderbird,
but ~25 Ubuntu `firefox-locale-*` / `thunderbird-locale-*` snap-transition
**stubs** (epoch `1:1snap1`) are half-installed (`iU`) and hard-depended-on by
the **`nvidia-system-station-apps`** desktop meta, so `full-upgrade` hard-stops
on unmet deps. **Fix (headless boxes):**

1. `apt purge` the locale stubs **plus** `nvidia-system-station
   nvidia-system-station-apps` in one transaction (27 packages; verified to
   touch no driver/CUDA/kernel/telemetry).
2. `apt purge firefox thunderbird xul-ext-ubufox`.
3. `dpkg --configure -a`, `apt -f install`, `full-upgrade` again → clean.

Do **not** run the 445-package `autoremove` while a GNOME session is live or
before confirming headless. After a reboot the orphaned
`nvidia-system-station*` app-station cleans itself up.

## 4. Host key — regenerate per unit

The factory image ships an **identical SSH host key on every unit** (castor and
pollux shipped the same ed25519 `root@localhost` key); host-key verification is
meaningless and keys collide. Per unit:

    sudo rm -f /etc/ssh/ssh_host_*key* && sudo ssh-keygen -A && sudo systemctl restart ssh

Do it in the **same ssh session** — an sshd restart doesn't drop the live
connection, so `cat` the new pubkey back before reconnecting. Then on the
controller: `ssh-keygen -R <name>` (and `-R <ip>`), and pin the new key under
the `HostKeyAlias` name. Don't pin `HostName` to the LAN address in the
controller's ssh config — see the unpin note in
[`cluster-ssh-remote-access`](./cluster-ssh-remote-access.md).

## 5. Stop spontaneous OTA/auto-upgrade reboots

The factory image enables `apt-daily-upgrade.timer` and
`nvidia-spark-run-apt-upgrade-once.service`. Background apt churn can peg the
box and (with an OTA) reboot it: pollux once wedged (sshd banner-exchange
timeout ~17 min while still answering ping — kernel up, userland starved) and
then rebooted itself with `fwupd` / `spark-ota-check` /
`nvidia-spark-run-apt-upgrade-once` running at boot. Unacceptable for a serving
node; harden in the bootstrap:

    systemctl disable --now apt-daily.timer apt-daily-upgrade.timer
    systemctl mask apt-daily.service apt-daily-upgrade.service
    systemctl disable nvidia-spark-run-apt-upgrade-once.service

(`systemd-sysupdate{,-reboot}.timer` already ship disabled — leave them.)
`last reboot` / `uptime` confirms whether it bounced.

## 6. Firmware — nothing to flash manually

`fwupdmgr get-updates` → all current (EC/UEFI/NVMe). DGX OTA is the vendor path:
`nvidia-spark-ota-check {is-ota-available,torn-score,summary}` (castor was on
the latest, OTA2607 July 2026, at bringup). A post-upgrade `torn-score` of
~2.6–6 is cosmetic on headless: failed checks are the deliberately purged
`nvidia-system-station{,-apps}` and desktop-theme drift;
`nvidia-firmware-580` / `USBPD` settle after the pending reboot
(`/var/run/reboot-required` = `nvidia-spark-limits`). Healthy-GPU baseline:
GB10, persistence Enabled, ~30–33 °C; **`nvidia-smi` shows `memory.total [N/A]`
— normal for unified Grace-Blackwell memory**, not a fault.

## 7. Fast-cable pairing (ConnectX QSFP, 200G RDMA)

Each Spark has a ConnectX-7 with two QSFP ports, **both cabled to the
partner** — 4 CX netdevs total. `enp1s0f0np0` / `enP2p1s0f0np0` are the live
200G DAC links (`ethtool` → `Speed: 200000Mb/s, Link detected: yes`;
`rdma link` → `rocep1s0f0` / `roceP2p1s0f0` ACTIVE); the `f1` ports are unused
(NO-CARRIER). Only **one** link is needed (llama.cpp RPC uses a single
connection).

- Config: persistent NetworkManager profile `cx-link0` on `enp1s0f0np0`,
  static point-to-point `/24` (the two ends' addresses in the overlay),
  **MTU 9000 (jumbo)**, **`ipv4.never-default yes`** so it never steals the LAN
  default route; `/etc/hosts` names `castor-cx` / `pollux-cx`. Survives reboot
  (autoconnect).
- Measured **111 Gbit/s** iperf3 (8 streams) — TCP tops out near half
  line-rate on the Grace ARM cores; plenty. llama.cpp's RPC backend
  auto-negotiates **true RDMA/RoCEv2** over these ports (`RDMA activated qpn=…
  mtu=4096` in the rpc-server log), not just TCP.
- castor's other NICs: `enP7s7` = 1GbE RJ45 (the LAN/ssh path), plus a down
  USB-ethernet and a MediaTek wifi.

## Networking bring-up trap

After a physical cable swap, pollux looked dead from the controller (no ping,
port 22 closed) while it could ping itself. The fault was **controller-side /
upstream router**: the controller had many aliased subnet IPs and *both*
Sparks, including already-working castor, went unreachable together. The tell
is **the control unit (castor) also failing**. The fix was router settings + a
wifi restart on the controller. If it is genuinely the Spark: `ip -br link`
(`LOWER_UP` = the live cable — a swap can move the link to an interface that
isn't the one holding the IP), `ip route` (need a default via the LAN
gateway), `sudo nmcli device connect <iface>` to re-DHCP. "Can't ping own LAN IP
but loopback works" = the IP's interface is down / no-carrier.

## 8. Ansible onboarding

Add the node to the overlay `hosts.yml` under `linux` (LAN `ansible_host`,
`node_role: serving`), deliberately **out of** the service groups
(inference / nfs_clients / llamacpp_hosts / agent_sandbox_hosts) until its
roles are wired. `playbooks/00-users.yml --limit castor,pollux` ran 2026-08-08Z:
canonical uid set present (ollama 805, nginx 807, bitwarden 808, agent_ro 811;
`agent_sandbox` / `hermes` correctly skipped, group-gated; prometheus/grafana
803/804 retired fleet-wide 2026-08-11Z); a hand-made `deploy` was verified
untouched and re-run is `changed=0`. The twins came up already matching the
gid == uid scheme, so the gid-pinning fix (which also fixed a macOS
staff/dialout bug; renumbering guard) was a no-op on them.

Not done at bringup (check overlay state before assuming): Tailscale join
(needs `vault_tailscale_authkey`; until then ssh is LAN-only with no off-subnet
fallback), `host_vars/<node>.yml` + `topology` capabilities + service-group
membership then the worker / embedder / llamacpp roles, and `nvcr.io` was
unreachable (TLS handshake timeout) for a `docker run --gpus` pull — check
registry/network/firewall before relying on NGC images. The standalone
llama.cpp endpoint is independent of precis inventory.
