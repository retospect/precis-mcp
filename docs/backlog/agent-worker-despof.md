# De-SPOF the melchior agent worker

plan_tick and the whole claude lane run on one melchior claude_inproc worker;
a hang stalls the lane cluster-wide (observed: a 100-deep plan_tick queue
starving ad-hoc jobs — decide transient-vs-chronic with a draining-vs-growing
sample; the pass is default-on, `registry.py` `default_profiles=_AGT`). Ops
levers: provision a second agent host (caspar/balthazar) with the OAuth state
+ an agent daemon (no code); co-location relief — get the ~73 G mlock'd
llama.cpp weight off the agent host (or drop `--mlock`) so jetsam stops
targeting the worker. Durable north star: the sandbox_run/claude_docker
substrate (docs/proposals/sandbox-run-substrate.md) subsumes both. See also
spark-agent-worker for the local-lane offload.
