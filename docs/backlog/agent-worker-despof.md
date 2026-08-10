# De-SPOF the melchior agent worker

plan_tick and the whole claude lane run on one melchior claude_inproc worker;
a hang stalls the lane cluster-wide (observed: a 100-deep plan_tick queue
starving ad-hoc jobs — decide transient-vs-chronic with a draining-vs-growing
sample; the pass is default-on, `registry.py` `default_profiles=_AGT`). Ops
levers: provision a second agent host (caspar/balthazar) with the OAuth state
+ an agent daemon (no code); co-location relief — get the ~73 G mlock'd
llama.cpp weight off the agent host (or drop `--mlock`) so jetsam stops
targeting the worker. Durable north star: the sandbox_run/claude_docker
substrate (`sandbox-run-substrate` (git-only)) subsumes both. See also
spark-agent-worker for the local-lane offload.

Evidence gr187627: `ssh_node`'s blocking dispatch starves the claiming
worker's whole pass rotation for the compute's runtime (heartbeat dark,
host-dark criticals flap).

Update 2026-08-09 (dispatch-stall incident, alert 199905): redundancy is
confirmed **zero** — `service_config` has `job_claude_inproc` prio=0 on
spark since 2026-07-18, so melchior is the sole claude-lane executor
fleet-wide. Same incident exposed the deeper decoupling: `resource_slots`
advertising (heartbeat auto-probe, no flag) and executor provisioning
(profile + `service_config`) have no coherence check — a model served only
on a non-executor host (qwen3-235b on caspar) plus the plan_tick `llm:`
affinity stamp stalled the lane for 16 h. Mitigated by the 10-min
claim-side affinity fallback (`LLM_AFFINITY_GRACE_MIN`,
`executors/_common.py`); the structural gap (nothing connects "who serves
a model" to "who can run jobs that want it") is still open and belongs to
whichever de-SPOF lever gets picked.
