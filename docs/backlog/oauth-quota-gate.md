# quota.evaluate() is dark — OAuth spend has no global gate

`src/precis/budget/meter.py` deliberately excludes OAUTH_TRANSPORTS
(claude_agent/claude_p) from the dollar meter as notional spend, deferring to
the quota snapshot — but `src/precis/budget/quota.py::evaluate` returns None
without a snapshot and nothing populates one. Net: `claude -p` spend is
invisible to every breaker except the planner guardrails. Either wire the
snapshot or fold OAuth spend into the meter behind its own cap.
Sonnet-shaped.
