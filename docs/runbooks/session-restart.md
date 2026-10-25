# Session restart — precis MCP missing, or restarting fleet sessions

**When.** A session's precis tools are absent (`search`/`get`/`more` not in
the tool list, or the SessionStart hook printed `precis MCP entry was
missing`), the user-scope `precis` entry vanished from `~/.claude.json`
(gr513664, 2026-10-24: a `/login` rewrite is the suspect), or a fleet
session has to be restarted for another reason.

## Diagnose

```bash
claude mcp get precis        # entry present? Status line says Connected?
scripts/mcp-entry-preflight --dry-run   # what the heal would do
```

The server being up is NOT evidence the entry exists: the server is a
shared streamable-http daemon (`deploy/mcp-http/precis-mcp-http-ensure.sh`),
the entry is a per-user line in `~/.claude.json` that only `claude mcp`
writes. On 2026-10-24 the server was healthy while every session ran
without the tools.

## Restore the entry

```bash
scripts/mcp-entry-preflight   # idempotent: re-adds only when missing
```

It runs `claude mcp get precis`, and only when that fails
`claude mcp add-json -s user precis "$(<ensure> --config)"` — the same
JSON `scripts/fleet`'s `wait_mcp` installs. It never edits
`~/.claude.json` by hand and never touches an existing entry.

A RUNNING session does not reload user-scope MCP config: a re-added
entry takes effect at that session's next start, never mid-session.

## Restart a session — check before you kill it

Sessions hold state a restart destroys. Before restarting any session
(yours or a fleet window):

1. `scripts/inflight` — is the tree mid-task, and who owns it?
2. Uncommitted work: `git status --porcelain` in its tree; background
   jobs (`get_output` shells, a running `scripts/ship`/`scripts/release`
   log, a `round` in progress — `scripts/round status`).
3. Pending decisions the session was shepherding (fleet review queue,
   unanswered `scripts/board` posts, messages it owed a coordinator).
4. Its session name — a restart may rename it (`chat-box-01` →
   `catpath-29` on 2026-10-24), so messages addressed to the old name
   go astray. Note the new name and re-point senders.

Restart order for a fleet-wide entry loss: the coordinator/master first
(its restart is the one that re-asks decisions), then panes mid-ship
(BUILDER), then idle panes. Each restarted session self-heals the entry
at start (SessionStart → `scripts/mcp-entry-preflight --for-hook` via
`scripts/mcp-http-install --for-hook`), so ordering is about lost work,
not repair order.

## If the entry keeps vanishing

A rewrite of `~/.claude.json` (a `/login` is the suspect) drops
`mcpServers`. If it recurs, capture the file's mtime around the trigger
and report upstream — gr513664 ask 3 is still open.
