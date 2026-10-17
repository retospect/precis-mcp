---
name: squash-audit
description: Audit the file list of a rebased squash commit before landing
metadata:
  type: feedback
---

After rebasing a squash commit onto main, diff its file list against the original branch; a conflict resolution can silently drop a file.
