---
description: "End-of-session wrap-up: commit WIP, sync onto main, gate on GitHub CI (scripts/ship --remote), squash-merge. Run from a feature worktree."
argument-hint: "[optional commit/ship message]"
allowed-tools: Bash(scripts/ship:*), Bash(git:*), Bash(docker:*), Bash(uv:*), Agent
---

<!-- precis-stub: land -->

# land

This skill lives in the precis corpus. Before any task work, read it
and follow it:

    get(kind='skill', id='land')

Search precis skills (`search(kind='skill', q='<goal>')`) before
starting any other task.

MCP down: stop the task. Retry the connection with growing waits
(1, 2, 4, 8 minutes, then every 15) and resume when it answers. Do
not work the task without skills.

Arguments: $ARGUMENTS
