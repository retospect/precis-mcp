---
name: flaky-rerun
description: A flaky test is rerun once, then filed as a gripe with its seed
metadata:
  type: feedback
---

If a test fails then passes on rerun, record the random seed and the xdist worker id and file a gripe; do not mark it skip.
