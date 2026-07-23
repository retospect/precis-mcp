# asa_slack — manual prep

This role deploys the daemon; it does **not** create the Slack app or seed
tokens (per-workspace, one-time, done by hand). Do this before running
`48-asa-slack.yml`:

## 1. Create the Slack app

At <https://api.slack.com/apps> → "Create New App" → From scratch, in the
target workspace.

**OAuth scopes** (OAuth & Permissions → Bot Token Scopes):
`chat:write`, `channels:history`, `groups:history`, `im:history`,
`mpim:history`, `channels:read`, `groups:read`, `im:read`, `users:read`,
`bots:read`.

**Socket Mode**: Settings → Socket Mode → enable. This generates the
app-level token (`xapp-...`) — grant it the `connections:write` scope.

**Event Subscriptions**: enable, subscribe to the bot event `message.channels`
(+ `message.groups`/`message.im`/`message.mpim` for private channels/DMs).

**Install the app** to the workspace (OAuth & Permissions → Install to
Workspace) — this mints the bot token (`xoxb-...`). Invite the bot into
whichever channels it should watch (`/invite @<app-name>`).

## 2. Seed the tokens into the vault (ADR 0055)

From a shell with `PRECIS_DATABASE_URL` set (or `--database-url`):

```bash
echo -n 'xoxb-...' | precis secret set ASA_SLACK_BOT_TOKEN
echo -n 'xapp-...' | precis secret set ASA_SLACK_APP_TOKEN
```

`load_slack_tokens` (`asa_slack/config.py`) resolves env → vault → file, so
once these are seeded the deployed daemon needs no token in its env or
config file at all — matching how `ASA_DISCORD_TOKEN` already works.

## 3. (Optional) pin an expected bot identity

The startup identity check (`asa_slack/identity.py`) is informational by
default — it logs whatever `auth.test` resolves to (an admin-assigned app
name that isn't "asa" is not an error). To make a wrong-token mismatch a
hard failure instead, set in inventory:

```yaml
asa_slack_expected_bot_user_id: "U0123456"   # the app's own bot user id
```

## 4. Channel allowlist (optional)

Empty (default) responds in every channel the app is invited into. To
restrict:

```yaml
asa_slack_allowed_channels: ["C0123456", "C0654321"]
```

## Prerequisite

Run `31-asa-bot.yml` on this host first (or alongside) — asa_slack reuses
the shared `/Users/hermes/.claude/mcp.json` (precis MCP config) and
`/Users/hermes/.asa/SOUL.md` that role deploys; it doesn't template its own.
