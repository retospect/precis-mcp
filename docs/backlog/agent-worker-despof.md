# De-SPOF the melchior agent worker (incl. the silent-outage thread)

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

Update 2026-08-19 — **topology changed; the SPOF did not.** The separate
`com.precis.worker-agent` daemon was retired ~07-22 and the agent profile
folded into the single `com.precis.worker` unit (`precis worker --profile
all --batch-size 32 --idle-seconds 2`); `job_claude_inproc` has run inside
it since ~08-07 18:34. Melchior is still the sole claude-lane executor, so
every lever above still applies — but the *diagnostic surface* moved:
`/var/log/precis-worker-agent.log` is a dead 0-byte file and the missing
daemon is expected, both of which read as "worker is dead" to a fresh
investigator (two agents in one session drew exactly that wrong
conclusion). Worth a line in whatever runbook covers the lane.

Live now: lane alive and polling, `claimed=0`, ~102 claude_inproc jobs
queued since 08-16 00:12 UTC never claimed — a **selection** failure, not
an outage, so the watchdog note below (dispatch-stall detector on
expired-lease job refs) should also cover "queue depth grows while
claimed=0", which no restart fixes.

Silent-outage thread (merged from agent-worker-silent-outage): melchior
`com.precis.worker-agent` was SIGKILL'd and stayed dead ~4 days
(2026-07-26→30), silently stalling all agent-profile work. Root-cause the -9
(jetsam/OOM/crashloop — the mlock'd llama.cpp weight above is the prime
suspect) so it can't recur silently; the deferred H1/H3/H4 reliability track
(memory `worker-agent-silent-outage`) is the same thread. Related watchdog:
verify the nursery dispatch-stall detector fires on expired-lease job refs.
