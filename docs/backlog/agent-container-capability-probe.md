# Deploy the container capability probe before trusting the container flip

`container_capability_ok()` (auth + bin-info + image-inspect, ~60 s cache,
fail-safe → in-proc) + the ~10-min `trip_container_unhealthy()` latch (OOM
137 / image-missing / daemon-unreachable → one in-proc retry) shipped on main
(e9c915ba) but are not deployed — they're the safety net that should go out
*before* flipping `precis_agent_container_enabled` anywhere. Also before a
gateway flip: smoke the dream×container interaction. Follow-ons noted in the
design: an empty-result assertion (cost0 ∧ turns0 ∧ 0-toolcalls ∧ no-text ⇒
raise+alert) and a /factory degraded-render of capability_ok. Owner
`src/precis/workers/executors/agent_container.py`.
