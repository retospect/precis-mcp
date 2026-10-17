---
name: migration-numbering
description: A new migration takes main's highest number plus one at landing time
metadata:
  type: project
---

Before landing a SQL migration, fetch origin/main and renumber the file to the highest existing number plus one; sealed migrations are never edited.

## Related

- [Ship gate red](ship-gate-red.md)
