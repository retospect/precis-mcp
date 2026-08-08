# finding-chase

## Residuals (from OPEN-ITEMS)

The forward-bridge pilot (`src/precis/workers/chase.py::_taproot_bridge`) is
LIVE on melchior's *system* worker (the pass is `default_profiles=_SYS`, not
the agent daemon), durable via the `precis_worker_taproot_chase` host_var.
Watch fingerprint: `links` rows with `set_by='chase'` (the manual on-ramp
writes `set_by='agent'`; baseline 0). Caveats: `PRECIS_CHASE_LLM=1` enables
the LLM verifier for ALL chase passes on that worker (capped by the daily
ceiling); the bridge only fires when a finding establishes — with zero
STATUS:tracing inflow it produces nothing, and canonical claim hubs are
excluded from the outbound chase, so evidence-empty hubs are NOT self-filled
(needs the backfill). Disable: flip the host_var + redeploy the worker role.
