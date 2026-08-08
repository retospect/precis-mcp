# Agentic jobs are claimed by hosts with no claude binary

`claude binary not found` errors (177/day at peak; still seen 08-07): work
claimed by a host that structurally cannot run it fails instead of being left
for a capable claimant. First establish which host (correlate
`worker_logs.host`, or add host to `llm_call_log` rows), then prefer a
claim-time capability gate (the `eligible`/`host_affinity` pattern dream_agent
already uses) over installing the binary fleet-wide — "this host can't do
this work" should be a claim-time fact. Not the vault OAuth cutover (every
occurrence predates it). Owner `src/precis/workers/dispatch.py` claim path +
`capability_probe.py`.

test: a host without the claude binary is never handed a
claude_p/claude_agent job.
