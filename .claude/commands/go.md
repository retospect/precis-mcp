---
description: "Implement the agreed spec, land it to main, cut a release branch from main, gate it on CI and deploy it. Run from a feature worktree."
argument-hint: "[optional ship/commit message]"
allowed-tools: Bash(scripts/ship:*), Bash(scripts/release:*), Bash(scripts/round:*), Bash(scripts/test:*), Bash(scripts/deploy:*), Bash(fleet:*), Bash(gh:*), Bash(cat:*), Bash(git:*), Bash(docker:*), Bash(uv:*), Bash(tail:*), Agent, Monitor, TaskStop
---

<!-- precis-stub: go -->

# go

This skill lives in the precis corpus. Before any task work, read it
and follow it:

    get(kind='skill', id='go')

Search precis skills (`search(kind='skill', q='<goal>')`) before
starting any other task.

MCP down: stop the task. Retry the connection with growing waits
(1, 2, 4, 8 minutes, then every 15) and resume when it answers. Do
not work the task without skills.

Arguments: $ARGUMENTS
