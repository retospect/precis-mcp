---
description: "Bring up or recover the thread-session fleet in tmux and take the orchestrator seat. Idempotent; run after a crash, inside tmux."
argument-hint: "[optional note, e.g. 'after crash' or a thread to leave out]"
allowed-tools: Bash(scripts/fleet:*), Bash(scripts/round:*), Bash(scripts/inflight:*), Bash(tmux:*), Bash(git:*), Monitor, Read, Write
---

<!-- precis-stub: fleet -->

# fleet

This skill lives in the precis corpus. Before any task work, read it
and follow it:

    get(kind='skill', id='fleet')

Search precis skills (`search(kind='skill', q='<goal>')`) before
starting any other task.

MCP down: stop the task. Retry the connection with growing waits
(1, 2, 4, 8 minutes, then every 15) and resume when it answers. Do
not work the task without skills.

Arguments: $ARGUMENTS
