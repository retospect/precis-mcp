---
description: "Quick-go: ungated squash-merge to main, deploy that sha, repair gate if a slot is free. Fast path; /go is the authoritative one."
argument-hint: "[optional commit/ship message]"
allowed-tools: Bash(scripts/ship:*), Bash(scripts/deploy:*), Bash(scripts/qgo-guard:*), Bash(scripts/mutate-diff:*), Bash(git:*), Bash(docker:*), Monitor, Agent
---

<!-- precis-stub: qgo -->

# qgo

This skill lives in the precis corpus. Before any task work, read it
and follow it:

    get(kind='skill', id='qgo')

Search precis skills (`search(kind='skill', q='<goal>')`) before
starting any other task.

MCP down: stop the task. Retry the connection with growing waits
(1, 2, 4, 8 minutes, then every 15) and resume when it answers. Do
not work the task without skills.

Arguments: $ARGUMENTS
