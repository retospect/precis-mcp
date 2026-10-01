# Deploying to the cluster, and proving what is live

**When.** Landing code on the fleet (`/go`, `scripts/deploy`), a per-host
manual install, or "is this host actually on current code?". Overlay changes:
[`cluster-overlay-sync`](./cluster-overlay-sync.md). Reaching nodes:
[`cluster-ssh-remote-access`](./cluster-ssh-remote-access.md). Worker
bounce/recovery: [`worker-jetsam-bootout-recovery`](./worker-jetsam-bootout-recovery.md).

## Why every host, every time

The nodes share the NAS (`/opt/nas` on Macs, `/nas` on Linux) — the same
`inbox`, `corpus` and `precis_prod` DB. Each host running `precis watch` races
the others on the shared inbox: whichever grabs a dropped PDF first processes
it. A watch-behaviour fix on ONE host loses to an old-code host. Deploy to
**all** hosts running `precis watch`; `scripts/deploy` pings every host first
and aborts if any is unreachable, so the cluster never runs mixed versions.

## Hosts and venvs (ownership)

Roles and logs: [`cluster-logs`](./cluster-logs.md). Inventory groups:
gateway = melchior, scheduler = balthazar, data = caspar.

| venv | where | notes |
|------|-------|-------|
| `/opt/precis/venv` (worker) | melchior, balthazar, castor, pollux | root-owned. Macs use pip; Linux venvs are **uv-created with NO pip module**. |
| `/opt/precis/embedder-venv` | hosts that run the embedder | same ownership. |
| `/opt/mcps/venv` (web + agent worker) | **melchior only** (gateway) | py3.14 vs `/opt/precis/venv` py3.12. |

- **caspar (data) gets neither the worker venv nor the embedder venv** —
  `redeploy-precis.yml` builds `_precis_venv_refs` to skip it ("it runs no
  precis daemon"). Any leftover caspar `/opt/precis/embedder-venv`, or a
  loaded `com.precis.embedder-watchdog` plist failing a restart cycle against a
  nonexistent job, is **vestigial** — it never converges and is not a deploy
  failure (cleanup tracked in gr260308).
- An `/opt/mcps` on scheduler/data hosts is a relic of an old topology:
  nothing loads it, `--refresh` never touches it, it drifts a release behind.
  Harmless, but reads as a failed deploy in a spot-check. The redeploy removes
  it idempotently off non-gateway hosts.
- All venvs are root-owned; install is from
  `git+https://github.com/retospect/precis-mcp@<commit>`.

## Canonical deploy: `scripts/deploy` or `/go`

`/go` = ship + deploy. `scripts/deploy` runs `redeploy-precis.yml` from the
**in-repo** `deploy/` tree (portable, cluster-agnostic, leak-gate-vetted;
`PRECIS_DEPLOY_FROM_TREE=1` is the default). `ansible.cfg` wires inventory,
the `deploy` user and `.vault-pass`; never `--ask-vault-pass`. `/redeploy` wraps
it (step 1 = `ansible all -m ping`).

- **Overlay** (real inventory + vault) = gitignored real files in the MAIN
  checkout's `deploy/inventory`, plus `deploy/.vault-pass` — **no symlinks**.
  `scripts/deploy` resolves it checkout-independently: this checkout's
  `deploy/inventory` if present, else the main checkout's (via
  `git --git-common-dir`), else `$PRECIS_OVERLAY_DIR` — so a deploy works from
  any worktree. **`deploy/.vault-pass` is the only key to the
  ansible-encrypted `vault.yml` — no git history, no remote. Back it up
  (password manager) or the vault is unrecoverable.**
- **Step 0 pins ONE commit.** A single `run_once` `git ls-remote` resolves the
  ref, broadcasts it, and `set_fact`s the three `precis_*_git_ref` install
  vars plus `precis_target_sha`. Install (`@<sha>`), the pre-flight
  bounce-gate and the convergence assert all use that frozen sha, so `main`
  advancing mid-deploy (the autonomous fixer ships continuously) cannot
  false-fail it: this run lands the pinned commit, the next picks up the newer.
  `-e precis_worker_git_ref=<branch> -e precis_web_git_ref=<branch>` still
  wins. Read the recap's sha or the venv's `direct_url.json` — never assume the
  deployed sha equals the one you gated.
- The play `uv --refresh-package`-reinstalls into all venvs, **embedder-first
  behind a `/healthz` gate** (the worker crash-loops if the embedder listener
  is down at boot), then force-bounces every `com.precis.*` launchd daemon and
  Linux systemd unit. The bounce is skipped when no venv actually moved.
- **Convergence assert** (end of play, per host): installed `direct_url.json`
  `commit_id` must equal the pinned sha or the redeploy fails loudly. A failure
  now means a genuinely skipped/failed install on that venv — re-run; it is
  *not* a moving `main`.
- **Code-only redeploys restart the daemon.** `roles/precis_worker`'s install
  task notifies both reload handlers (OS-guarded); `--refresh-package` makes
  the install report *changed every run*, so the worker bounces on every play
  (cheap — an orphaned claim is swept). Before this, a push updated the venv on
  disk while the worker ran stale in-memory code for days.
- **`redeploy-precis.yml` does not re-template the worker-agent plist.** That
  is `playbooks/37-precis-worker-agent.yml` only (via `site.yml`). After adding
  any agent-worker env var, run
  `ansible-playbook -i inventory/hosts.yml playbooks/37-precis-worker-agent.yml`
  or the var lands everywhere except the agent worker.
- ansible's **pipelining** hangs intermittently against castor/pollux (remote
  become python polls stdin forever for the module payload):
  `ANSIBLE_PIPELINING=False` fixes it immediately — try it before debugging
  any "hung deploy" on the twins (gripe filed 2026-08-29Z).

## Before you launch one: you may not need to

`scripts/deploy` takes a **cross-worktree lock** (`<git-common-dir>/precis-deploy.lock`;
`mkdir` as the atomic acquire since macOS has no `flock`; steals a dead
holder's lock; waits up to 1800 s then dies). Concurrent sessions serialize,
but a sibling's run **deploys `main`, i.e. your commits too** if they are
ancestors. First `ls -t .deploy-logs/ | head` for an in-flight or just-finished
run, and compare the live `direct_url.json` `commit_id` to your commit. A
sibling's 11-minute run had already shipped one fix; a second deploy would
have bounced every daemon for nothing.

⚠ **Never pipe `scripts/deploy` (or `ship`, `bump`) into `tail`/`head`/`grep`
— the pipeline's exit code is the filter's.** A run that aborted on a precheck
reported "exited with code 0" and read as success. Redirect to a log, read the
`✖` line, or `set -o pipefail`.

## Preflight traps

1. **Catpath wheel precheck looks in `~/catpath`, not your real checkout.**
   Deploy dies with "autocatpath floor is >=X but no wheel that new exists in
   …/dist and there is no catpath checkout there" (gr263082). Set
   `PRECIS_CATPATH_DIR=<catpath checkout>`, or
   `PRECIS_DEPLOY_SKIP_CATPATH_WHEEL=1` when hosts are already seeded.
   Shipping catpath itself: [`catpath-cluster-ship`](./catpath-cluster-ship.md).
2. **Catpath checkout ahead of `uv.lock`'s pin blocks preflight** ("would
   build packaged code that differs from the commit uv.lock pins … both builds
   labelled X"). Don't move the shared checkout (other sessions use it): add a
   detached worktree at the pinned sha from the checkout
   (`git worktree add --detach /tmp/catpath-pin-<tag> <pinned-sha>`), run
   `PRECIS_CATPATH_DIR=/tmp/catpath-pin-<tag> scripts/deploy`, remove the
   worktree after. The pinned sha is in `uv.lock`
   (`source = { git = "…catpath?branch=main#<sha>" }`).
3. **You cannot deploy the exactly-gated sha once a sibling qlands mid-gate.**
   `scripts/deploy <sha>` refuses any ref that is an ancestor of `origin/main`
   (gr338201 rollback guard), not just of the deployed sha. Choice: deploy
   `main` with the ungated tail (fine if docs/small; say so in the report), or
   gate again.
4. First ansible ping can time out on a host right after Tailscale starts —
   `tailscale ping <host>` warms the path; re-run.

## Manual per-host fallback

Install (root-owned venvs, `--force-reinstall --no-deps`):

- Mac (melchior/balthazar): `sudo /opt/precis/venv/bin/pip install
  --force-reinstall --no-deps "git+https://github.com/retospect/precis-mcp@<commit>"`;
  melchior web: same with `/opt/mcps/venv/bin/pip`.
- Linux (castor/pollux): `sudo /home/deploy/.local/bin/uv pip install --python
  /opt/precis/venv/bin/python --reinstall --no-deps "git+…@<commit>"` (uv is
  not on PATH; the venv has no pip).

Restart after install — **always restart, don't just reinstall:**

- Mac launchd: `sudo launchctl kickstart -k system/com.precis.<svc>`
  (`svc` = watch/worker/web). `bootout`+`bootstrap` is needed only for
  plist/env changes and intermittently throws
  `Bootstrap failed: 5: Input/output error` — retry after a few seconds.
- Linux systemd: `sudo systemctl restart precis-watch precis-worker`.

A long-running daemon that imported a module **before** an in-place reinstall
keeps the OLD code for its in-process work, even though freshly spawned
subprocesses get the new code. Verify loaded code with
`python -c "import precis.cli.watch as w,inspect; print('<marker>' in inspect.getsource(w.<fn>))"`.
After a redeploy, a traceback can render NEW source text at the OLD line
offsets if the process had not fully swapped — don't trust a traceback's
source lines; confirm the fix is live by watching the prod error rate go to
zero (frozen `max(ts)`).

## Which commit is a host actually running

Read the **installed venv's** `direct_url.json`, never the uv checkout cache:

    /opt/{precis/venv,precis/embedder-venv,mcps/venv}/lib/python*/site-packages/precis_mcp-*.dist-info/direct_url.json

→ `vcs_info.commit_id` (it also carries the real version). Probe recipe and the
zsh-glob caveat: [`cluster-ssh-remote-access`](./cluster-ssh-remote-access.md).

- **Trap:** `~deploy/.cache/uv/git-v0/checkouts/<hash>/<sha>/.git/HEAD` can lag
  the installed wheel by **weeks** — uv keeps one working checkout and reuses
  it. It once made the whole cluster look stale when every managed venv was on
  `main` HEAD. `uv cache prune` reports "No unused entries found" while a
  version is installed, so there is usually no cache cruft to clean.
- ⚠ **Version is not a deploy identity** (gr270998). Two hosts reported the
  same `precis_mcp` version while running different commits. A same-version
  install can no-op and leave the old commit ([`catpath-cluster-ship`](./catpath-cluster-ship.md)
  "same-version wheel = host no-op"). Any `pip list` / dist-info-version
  spot-check will call a drifted host current — always compare
  `commit_id`. Whether the convergence assert should have caught that case was
  unresolved; check first when picking up gr270998.
- Don't trust `scripts/ship`'s deploy-lag footer as fleet ground truth — see
  [`cluster-ssh-remote-access`](./cluster-ssh-remote-access.md).

## Linux GPU node specifics

- **DFT / GPU-relax.** ADR 0043's `struct_relax` job runs the `precis-dft:cpu`
  container. Wiring: `deploy/roles/dft` + `deploy/playbooks/42-dft.yml`:
  adds `deploy` to the docker group, creates `/shared/scratch`, drops
  `/etc/precis/dft.env` (`PRECIS_NODE=<node>` + `PRECIS_DFT_*`) via a systemd
  drop-in `10-dft.conf` (EnvironmentFile), asserts the image is present,
  restarts the worker. Deploy order: `playbooks/20-precis-worker.yml --limit
  <node>` (pulls `struct_relax` into the venv) **then** `42-dft.yml`. Image
  build is optional (`-e dft_build_image=true`); the image + CUDA base were
  already on the node, and Docker Hub is unreachable from the cluster so the
  base can't be re-pulled. Verified originally on `spark`; the GPU-pinned
  `dft`/`fold` passes now live on `pollux` ([`cluster-logs`](./cluster-logs.md))
  — confirm the target group in the overlay.
- **`deploy` gates docker behind sudo/group.** `docker image inspect` as
  `deploy` returns a permission-denied that *looks like* "image absent" — use
  `sudo -n docker images` to see the truth.
- **PAW-dataset gap (2026-07):** `build_run_argv` mounts only in/out, not
  `GPAW_SETUP_PATH`, so a true `gpaw` DFT relax fails for lack of PAW setups
  (an `ml`/MACE relax is fine). Check whether it still holds before relying on
  `gpaw`.
