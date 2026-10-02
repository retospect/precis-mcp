---
status: idea
pillar: platform
---

# Backlog groomer — the work-items half

The gripe → fix_gripe-todo groomer shipped (`src/precis/workers/
backlog_groom.py`, default-OFF). The items half is blocked on two prereqs:
docs/backlog/ items aren't packaged into the wheel, so a deployed worker
can't read them (needs a packaged or DB-backed backlog source), and there is
no `build_feature` job_type for a free-text feature item. The groomer is
OFF on prod by ruling (2026-10-02, `service_config` melchior/`backlog_groom`
prio 0; sessions fix gripes, not the lane), so this half has no consumer
until Reto re-arms it.
