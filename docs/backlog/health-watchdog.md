# health-watchdog

## Residuals (from OPEN-ITEMS)

- Out-of-band DB-liveness monitor: the 2026-07-05 ~8 h prod outage ran
  unalerted because every alerting path is DB-backed — an external SELECT 1
  watcher on a different host (fixer host / laptop cron) → Discord on
  failure; a worker-log-volume trend alarm is a cheap second signal.
- Set PRECIS_OPS_ALERT_TARGET on system-profile workers — critical push is
  dark until then (worker-restart/dead-worker alerts land only in /alerts).
- Reto want: a periodic ops agent that auto-gathers status (services, APIs,
  db load, fs space, memory, temperatures, odd log entries; are queues
  working, are we ingesting/classifying — maybe a status kind,
  view='all relevant') and has an LLM judge reasonability; plus an
  are-we-working-on-the-right-things prioritization agent.
