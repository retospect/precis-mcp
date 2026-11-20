---
description: "Coordinate one peer round: deploy the open release head with a fresh green CI verdict, verify, open the next round. Run from the deploy session."
argument-hint: "[optional note for the round ping]"
allowed-tools: Bash(scripts/round:*), Bash(scripts/ship:*), Bash(scripts/deploy:*), Bash(scripts/inflight:*), Bash(scripts/qgo-guard:*), Bash(git:*), Bash(cat:*), Agent, SendMessage, ListAgents
---

<!-- precis-stub: round -->

# round

This skill lives in the precis corpus. Before any task work, read it
and follow it:

    get(kind='skill', id='round')

Search precis skills (`search(kind='skill', q='<goal>')`) before
starting any other task.

MCP down: stop the task. Retry the connection with growing waits
(1, 2, 4, 8 minutes, then every 15) and resume when it answers. Do
not work the task without skills.

Arguments: $ARGUMENTS
