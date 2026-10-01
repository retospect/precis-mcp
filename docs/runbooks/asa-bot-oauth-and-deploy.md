# asa-bot: Claude auth and deploy

**What it is.** asa-bot (`src/asa_bot/`; the Discord bridge) runs Asa as a
Claude Code agent on **melchior** as user **hermes** (LaunchDaemon
`com.asa.bot`, log `/Users/hermes/.asa/asa-bot.log`, needs `sudo`). Each Discord
turn spawns a fresh `claude -p` subprocess (`src/asa_bot/claude_invoke.py::invoke`).

## "Failed to authenticate." on every turn

Cause (2026-07-13Z): the subprocess inherited env but set no
`CLAUDE_CODE_OAUTH_TOKEN`, so `claude` fell back to the interactive keychain
credential `/Users/hermes/.claude/.credentials.json`, which is **short-lived
(~1 day)** and had expired (`claude: Not logged in · Please run /login`). The
long-lived token (from `claude setup-token`, ~1 year) was fine — asa just
wasn't wired to it.

Fix: `src/asa_bot/oauth.py::ensure_oauth_token(env)` fills
`CLAUDE_CODE_OAUTH_TOKEN` (idempotent, override-safe), called in `invoke()`
after building the subprocess env. It mirrors precis's `utils/claude_oauth`
(asa can't import precis; separate venv).

**Source of the token (changed 2026-08-07Z):** both mirrors now resolve
env → **DB vault** → `~/.secrets/pw/<NAME>`. The per-user
`~/.claude_oauth_token` leg is **gone** from the code and the files are purged
fleet-wide by redeploy step 0a2 (melchior `deploy` + `hermes`, and the Spark
atomsim user). **Don't "fix" an auth failure by recreating that file** — it no
longer resolves, and while it existed it sat *ahead* of the vault and silently
shadowed rotation. Rotate/repair with `precis secret set
CLAUDE_CODE_OAUTH_TOKEN` (picked up within the 60 s secrets cache, no deploy):
[`rotate-agent-rw-credential`](./rotate-agent-rw-credential.md)
§`CLAUDE_CODE_OAUTH_TOKEN`.

## Deploy

asa-bot deploys with **ansible, not precis's `scripts/ship`**. The repo is a
laptop-local checkout with no git remote; melchior-only.

    cd <precis-mcp>/deploy && SSH_AUTH_SOCK= ansible-playbook playbooks/31-asa-bot.yml

Ansible runs from the repo `deploy/` tree (overlay = gitignored real files in
`deploy/inventory`). The play rsyncs the asa-bot working tree to `/opt/asa/src`
(editable install) and restarts the daemon; runtime source lives at
`/opt/asa/src/src/asa_bot/`. **Unset `SSH_AUTH_SOCK`** (or add
`-o IdentityAgent=none`): the flaky ssh-agent makes rsync fail
`communication with agent failed` —
[`cluster-ssh-remote-access`](./cluster-ssh-remote-access.md).

## Bootout hazard (guarded)

The deploy's restart handler once left `com.asa.bot` de-domained
(`launchctl print system/com.asa.bot` empty, no process) while reporting
success — the same jetsam/bootout signature as the workers
([`worker-jetsam-bootout-recovery`](./worker-jetsam-bootout-recovery.md)).
Manual recovery: `sudo launchctl bootstrap system
/Library/LaunchDaemons/com.asa.bot.plist`.

Hardened in `deploy/roles/asa_bot/tasks/main.yml`: it ends with
`meta: flush_handlers` and a "Verify asa-bot is running" gate that settles,
asserts `pid = <n>`, self-heals one `bootstrap` on de-domain, and fails the
deploy RED if asa never comes up — a dead bot can't hide behind a green deploy.
asa's plist already carries `ProcessType=Interactive`.

Non-fatal boot-log noise: `ModuleNotFoundError: No module named 'precis'` from
the DB-log-handler attach — "continuing without DB logs"; ignore.
