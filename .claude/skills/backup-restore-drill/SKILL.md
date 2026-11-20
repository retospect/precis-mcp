---
name: backup-restore-drill
description: "Use to test prod Postgres backups or answer 'could we actually restore?': restores a nightly dump into a throwaway container, verifies, times, tears down."
---

<!-- precis-stub: backup-restore-drill -->

# backup-restore-drill

This skill lives in the precis corpus. Before any task work, read it
and follow it:

    get(kind='skill', id='backup-restore-drill')

Search precis skills (`search(kind='skill', q='<goal>')`) before
starting any other task.

MCP down: stop the task. Retry the connection with growing waits
(1, 2, 4, 8 minutes, then every 15) and resume when it answers. Do
not work the task without skills.
