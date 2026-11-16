---
description: "Mine recent agent sessions and prod tool-call logs for where the precis MCP surface confused agents or lacked capability; attach evidence to backlog or file a gripe."
argument-hint: "[window, e.g. '7d' or '14d' — default 14d]"
allowed-tools: Read, Grep, Glob, Edit, Write, Bash(scripts/mine-sessions/run.sh:*), Bash(uv run scripts/mine-sessions/*), Bash(scripts/surface-review:*), Bash(scripts/docs-index:*), Bash(git log:*), Bash(git show:*), Bash(git merge-base:*), Bash(grep:*), Bash(ls:*), Bash(wc:*), Task, mcp__precis__get, mcp__precis__search, mcp__precis__put
---

<!-- precis-stub: surface-review -->

# surface-review

This skill lives in the precis corpus. Before any task work, read it
and follow it:

    get(kind='skill', id='surface-review')

Search precis skills (`search(kind='skill', q='<goal>')`) before
starting any other task.

MCP down: stop the task. Retry the connection with growing waits
(1, 2, 4, 8 minutes, then every 15) and resume when it answers. Do
not work the task without skills.

Arguments: $ARGUMENTS
