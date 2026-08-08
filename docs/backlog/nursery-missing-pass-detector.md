# Nursery: detect an env-gated pass silently absent from a live rotation

A ServiceSpec env-gate mismatch left quest_loop_reconcile registered but
skipped every cycle for days, unflagged, stalling the agent lane. Add a
nursery/health check that flags a known env-gated pass missing from a live
worker's rotation for N hours. Also: quest_tick / catpath_explore never
persist `meta.transcript` (a confusion-mining blind spot) — add it. Owner
`src/precis/workers/nursery.py`.
