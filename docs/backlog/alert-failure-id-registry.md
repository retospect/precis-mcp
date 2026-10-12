---
status: ready
pillar: platform
title: alert failure ids — remainder: agent verb to ack
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
rows dimmed with ack/unack buttons. Shipped 2026-10-12
(`refactor(alert): /status onto health_panel; call-site rule constants`):
the Health sub-tab renders `health_panel()` — the same object the agent's
`/health` reads (`/status/backlog` still computes live); every producer's
`source=` spelling binds `RULES_BY_ID["<rule id>"].rule` instead of a local
literal (the totality test resolves the binding and gates each key names a
registered id). What is left: an agent verb to ack (the web
tab and `ack_alert` are the only entry points).

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
