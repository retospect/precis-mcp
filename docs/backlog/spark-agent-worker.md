# Run the local-model job lane on a spark worker, not melchior

The claude_inproc pin to melchior is obsolete for operations steered onto the
BIG chain: the in-process openai_tools loop needs only a DB connection and
HTTP reach to the local endpoint — no claude binary, no OAuth. Reto's call:
make a serving spark (castor/pollux) a precis worker pulling these jobs. Work
out: node choice (the worker competes with llama.cpp RPC cores; the
nice-all-jobs core reservation is the lever); eligibility = "can reach the
local endpoint", not a hostname (the scheduler's `eligible` callable), else
it trades one SPOF for another; the residual claude lane (fix_gripe etc.)
must keep landing on melchior — a second eligible host for one lane, not a
profile migration. Owner `deploy/` host profiles +
`src/precis/workers/registry.py`.

test: a plan_tick/briefing job completes on a spark-node worker with no OAuth
credential present.
