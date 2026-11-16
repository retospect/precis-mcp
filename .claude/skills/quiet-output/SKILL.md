---
name: quiet-output
description: "Before a verbose dev command (deploy, image build, ingest, migration, big git/gh/psql/grep/find output), run it through rtk so only signal reaches context."
---

<!-- precis-stub: quiet-output -->

# quiet-output

This skill lives in the precis corpus. Before any task work, read it
and follow it:

    get(kind='skill', id='quiet-output')

Search precis skills (`search(kind='skill', q='<goal>')`) before
starting any other task.

MCP down: stop the task. Retry the connection with growing waits
(1, 2, 4, 8 minutes, then every 15) and resume when it answers. Do
not work the task without skills.
