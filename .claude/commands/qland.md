---
description: "Quick-land: commit your work, sync, ruff + secret scan + fast DB-free guards (mypy, import contracts and all pytest run at the release gate), squash-merge without pytest. For burst-landing many worktrees; finish with one /go."
argument-hint: "[optional commit/ship message]"
allowed-tools: Bash(scripts/ship:*), Bash(git:*), Agent
---

<!-- precis-stub: qland -->

# qland

This skill lives in the precis corpus. Before any task work, read it
and follow it:

    get(kind='skill', id='qland')

Search precis skills (`search(kind='skill', q='<goal>')`) before
starting any other task.

MCP down: stop the task. Retry the connection with growing waits
(1, 2, 4, 8 minutes, then every 15) and resume when it answers. Do
not work the task without skills.

Arguments: $ARGUMENTS
