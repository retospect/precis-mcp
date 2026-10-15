---
status: idea
pillar: platform
title: alert failure ids — read-cost revisit triggers
prio: low
---

# Alert failure ids — revisit triggers

Everything this item specified has shipped: the rule catalogue
(`src/precis/alert_ids.py`, `/rules`, `/health`, `<source>/<fingerprint>`),
`acked_until` TTL acks, and (2026-10-15, gr489245) the agent's ack through
the tag verb, `tag(kind='alert', id=N, add=['ack:6h'])` (skill
`precis-alert-help` §Triage, `tests/test_alert_ack_tag.py`). What remains
is two conditional performance fixes, to do only when a trigger fires.

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
