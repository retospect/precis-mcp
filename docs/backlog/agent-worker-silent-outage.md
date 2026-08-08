# worker-agent daemon silent outage — root-cause the -9

melchior `com.precis.worker-agent` was SIGKILL'd and stayed dead ~4 days
(2026-07-26→30), silently stalling all agent-profile work. Investigate the -9
(jetsam/OOM/crashloop) so it can't recur silently. The news wire still
composed ~2 h late after the H2/H5 fixes — the deferred H1/H3/H4 reliability
track (memory `worker-agent-silent-outage`) is the same thread. Related
watchdog: verify the nursery dispatch-stall detector fires on expired-lease
job refs. Ops + `src/precis/workers/`.
