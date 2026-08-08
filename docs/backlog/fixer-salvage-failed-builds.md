# fixer-salvage-failed-builds

## Residuals (from OPEN-ITEMS)

Pair push-on-NEEDS_YOU with branch GC so half-built branches don't
accumulate (or keep the failing worktree under .fixer-work/ with a report
pointer). Related fixer ops, batched here: stale branches to clean —
fix/smoke, fix/build-prompt-map-freshness, fix/fixer-persistent-log,
fix/launchd-smoke (origin) + fix/shippath (local), needs Reto's OK;
PRECIS_FIXER_DISCORD_WEBHOOK unset, so loud NEEDS_YOU reports are log-only;
the agentic post-deploy followup is a /readyz stub, not a real
look-at-prod-and-fix-forward pass. Owner `src/precis/fixer/tick.py`.
