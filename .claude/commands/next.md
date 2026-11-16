---
description: "Summarize next steps; emit a /compact retention argument and a copy-paste recovery prompt. Use before compacting or for a clean restart point."
argument-hint: "[optional steer — where you want to go next, e.g. 'do the factory with the flubber']"
allowed-tools: Bash(git:*), Read, Glob, Grep, mcp__precis__get, mcp__precis__search
---

<!-- precis-stub: next -->

# next

This skill lives in the precis corpus. Before any task work, read it
and follow it:

    get(kind='skill', id='next')

Search precis skills (`search(kind='skill', q='<goal>')`) before
starting any other task.

MCP down: stop the task. Retry the connection with growing waits
(1, 2, 4, 8 minutes, then every 15) and resume when it answers. Do
not work the task without skills.

Arguments: $ARGUMENTS
