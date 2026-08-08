# Backlog groomer — the work-items half

The gripe → fix_gripe-todo groomer shipped (`src/precis/workers/
backlog_groom.py`, default-OFF). The items half is blocked on two prereqs:
docs/backlog/ items aren't packaged into the wheel, so a deployed worker
can't read them (needs a packaged or DB-backed backlog source), and there is
no `build_feature` job_type for a free-text feature item. Activation (ops):
flip PRECIS_BACKLOG_GROOM_ENABLED=1 on a system worker to drain open gripes;
watch mint count + fixer throughput before widening.
