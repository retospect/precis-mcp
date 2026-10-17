---
name: secret-gate
description: Cluster addresses and vault blobs are gated across the whole public tree
metadata:
  type: project
---

The deploy-tree secret test scans every tracked file for tailnet and LAN address ranges; a genuine sample takes an allow marker with a reason.
