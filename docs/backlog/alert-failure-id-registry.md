---
status: ready
pillar: platform
title: alert failure ids — remainder: /status onto the panel, call-site constants
prio: normal
model: sonnet
---

# Alert failure ids — the remainder

Shipped 2026-10-09 (`feat(alert): registered failure ids, /rules catalogue
and an idle-aware health panel`): `src/precis/alert_ids.py` (the rule
catalogue, `resolve()`), `raise_alert` stamping `meta.rule_id` and refusing
`/` in a source, `health_digest` pinned fingerprints
(`_PINNED_FINGERPRINTS`) + the hourly checks snapshot
(`health_digest:checks_snapshot`) + `health_panel()`, and on `AlertHandler`
`id='/rules'`, `id='/health'` and `id='<source>/<fingerprint>'`
(`alert_history`). Totality gate: `tests/test_alert_ids.py`. Skill routing:
`precis-alert-help` §Health questions, `precis-status-help`, the doctor
prompt and the `cluster-ops` remit. Also shipped (`feat(alert): acked_until
TTL`): `alerts.ack_alert` / `unack_alert` store `meta.acked_until` on a
still-open row; `ACKED_SQL` filters it in `list_open_alerts`, the nav badge,
`_check_alert_backlog_rot` and the remediation router; `/alerts` shows acked
rows dimmed with ack/unack buttons. Not built: an agent verb to ack (the web
tab and `ack_alert` are the only entry points). What is left:

## 1. `/status` onto the panel (optional)

`precis_web/routes/status.py` still computes its liveness sections itself
(through the same `health_checks` functions, so there is one truth already).
Refactoring it onto `health_digest.health_panel()` would make the web page
and the agent read literally the same object; `/status/backlog` keeps
computing live for a human who clicks it. Confirm by diffing a page render
before/after. Skipped at ship to keep the diff to the agent surface.

## 2. Call-site constants

Producers still spell their `source=` as local constants
(`_ALERT_SOURCE = "admit:oversize"` etc.); the static test resolves and
checks them, so nothing drifts, but importing the registered rule's source
from `precis.alert_ids` at each of the ~20 sites would make the link
explicit in the code. Mechanical; pair with any touch of those files.

## Notes carried forward

* `alert_history` counts recurrence over all `refs` rows sharing
  `(alert_source, fingerprint)`; only the open-row partial index exists
  (0099). Fine today; if the single-id read exceeds the 500 ms p95 budget
  the fix is a plain index on `(alert_source, fingerprint) WHERE kind =
  'alert'`.
* D3 revisit triggers for the panel: aggregate DB time >500 ms p95, the MCP
  read sustained >10 calls/min, or `worker_logs` handler rows past ~2M/7d.
  Cheap win if needed: replace the `max(created_at)` freshness probes with
  `ORDER BY <pk> DESC LIMIT 1` (~310 ms → ~30 ms; check composite-pk
  semantics on `chunk_embeddings` / `chunk_summaries` first).
