---
description: "Review all worktrees/branches in flight and clean up the safe ones; ambiguous dirty trees come back for judgment."
allowed-tools: Bash(scripts/inflight:*), Bash(scripts/reap-worktrees:*), Bash(git:*), Agent
---

<!-- precis-stub: workspace-cleanup -->

# workspace-cleanup

This skill lives in the precis corpus. Before any task work, read it
and follow it:

    get(kind='skill', id='workspace-cleanup')

Search precis skills (`search(kind='skill', q='<goal>')`) before
starting any other task.

MCP down: stop the task. Retry the connection with growing waits
(1, 2, 4, 8 minutes, then every 15) and resume when it answers. Do
not work the task without skills.
