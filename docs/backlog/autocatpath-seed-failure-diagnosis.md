# Read the first unbuffered autocatpath_seed failures (the unresolved 59)

Diagnosis pass; evidence exists only since the `80e562a6` deploy (unbuffered
child stdout). Pull `chunks.text` (`chunk_kind='job_event'`) for `kind='job'`
refs with `meta->>'job_type'='autocatpath_seed'` created after the deploy and
read the stdout tail. Target: the 59 runs dying 21 s–8061 s with "child
process exited without writing result.json" (rc=-15 siblings) — cause
explicitly unnamed; do not pick a remedy before reading. Evidence gr192371:
seed jobs die "child exited without result.json" (e3nn/mace torch.load
warnings), 9 failed vs 6 succeeded. Needs Reto first: set
`PRECIS_PATHWAY_KEEP_FAILED_SCRATCH=1` on spark (flag has no TTL/cap — sweep
or unset after), and decide spark's nightly-reboot.timer + enabled apt-daily
timers (gr50907) — a node rebooting under a 3 h job may be the whole answer.
Also unexplained: job ref 187387 has zero chunks where every sibling has
diagnostics. Owner `src/precis_pathway/runner.py` diagnostics.
