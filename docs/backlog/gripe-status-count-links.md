---
status: in-progress
pillar: platform
title: clickable gripe status counts
prio: medium
---

# gr468087 — approved Reto navigation request

Native open/unclaimed; claimed wip for R14. Existing summary renders counts
as plain text, and route normalizes status=open/triaged/in_review to live.
Use existing /gripes?status= navigation for live total and each status group,
accept the existing gripe workflow vocabulary and bind the exact-status SQL
filter. Keep live/terminal/all meanings unchanged; no write/API/schema path.
Regression uses real synthetic PG rows plus rendered HTTP anchors: follow
every count and verify only that status, live excludes done/wontfix, invalid
status safely falls back to live. Root owns version/full gate/deploy.
