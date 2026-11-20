---
description: Implement the agreed spec, ship to main with the local gate, deploy the gated sha to the cluster (scripts/deploy --pinned).
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
