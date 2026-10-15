# Fleet session-hook push

**When.** `get(kind='fleet')` should show a session the host collector
cannot see (the launchd reporter is not installed, or the session leaves
no transcript/pane trace), or should carry a session's own state and
context fill fresher than a 60 s scan. Mechanism: `scripts/fleet-report
--hook --push` sends a one-row *partial* report for the calling session
(gr486948, 2026-10-15).

## Semantics

- A partial report upserts its own row and refreshes the host row's
  `reported_at`; it never dead-marks absent rows and never overwrites
  host `quota`/`exceptions_hash` it did not measure.
- Its rows carry `via: "hook"`: a full host report patches fields it
  sees but leaves liveness to the session — SessionEnd pushes `dead`. A
  crashed session's row simply ages (`last_active` grows stale in the
  AGENTS line). A full report that sees the same key patches `via` back
  to absent and owns liveness again.
- Row content: state (`--state` wins over the `hook_event_name` map),
  ctx% (own `$TMUX_PANE` footer, else the Claude transcript's
  `transcript_path`), tmux target + attach, worktree branch/purpose/
  dirty/ahead/behind. Outside a git worktree it is a no-op.
- Token: same as the collector — `PRECIS_MCP_TOKEN` or
  `PRECIS_MCP_TOKEN_FILE`; `PRECIS_FLEET_MCP_URL` overrides the endpoint.
- Hook mode never breaks the session: push failures go to stderr and the
  exit code stays 0. `PRECIS_FLEET_HOOK=0` disables the push entirely.

## Wiring

Devin is wired by `.devin/hooks.v1.json` in this repo
(SessionStart/UserPromptSubmit → `working`, PermissionRequest →
`waiting`, Stop → `idle`, SessionEnd → `dead`).

Claude needs the same entries under `hooks` in `.claude/settings.json`
(user-owned, not committed — an operator adds them):

```json
{
  "SessionStart": [
    {"hooks": [{"type": "command",
      "command": "\"$CLAUDE_PROJECT_DIR/scripts/fleet-report\" --hook --vendor claude --state working --push || true"}]}
  ],
  "UserPromptSubmit": [
    {"hooks": [{"type": "command",
      "command": "\"$CLAUDE_PROJECT_DIR/scripts/fleet-report\" --hook --vendor claude --state working --push || true"}]}
  ],
  "Stop": [
    {"hooks": [{"type": "command",
      "command": "\"$CLAUDE_PROJECT_DIR/scripts/fleet-report\" --hook --vendor claude --state idle --push || true"}]}
  ],
  "SessionEnd": [
    {"hooks": [{"type": "command",
      "command": "\"$CLAUDE_PROJECT_DIR/scripts/fleet-report\" --hook --vendor claude --state dead --push || true"}]}
  ]
}
```

Codex has no lifecycle-hook equivalent today; its sessions stay on the
host collector.

## Verify

```
scripts/fleet-report --hook --json        # the one-row report, no push
get(kind='fleet')                          # the row under AGENTS
get(kind='fleet', id='<vendor>:<host>:<project>:<tree>')
```

`via: hook` shows on the row's meta while a hook owns its liveness.
