---
status: ready
pillar: platform
title: alert failure ids — remainder: acked_until TTL, /status onto the panel, call-site constants
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
prompt and the `cluster-ops` remit. What is left:

## 1. `acknowledged`, stored as a TTL on a still-open row

Today the only way to silence a known-failing id is
`tag(add=['alert-state:resolved'])`, which asserts something false and is
undone the next time the producer runs. Design (D4, resolved 2026-09-25):
the ack is **not** a third state — the row keeps `alert-state:open` and
`resolved_at IS NULL`, and an unindexed `meta.acked_until` (+ `acked_by`)
is filtered by *readers*. Ack is orthogonal to state, so dedup keeps
bumping the row and `resolve_stale_alerts` still auto-closes it when the
condition clears.

Why not a third `alert-state:` value or a column: `raise_alert` dedups on
the **tag**, the unique index `uq_alert_open_source_fingerprint` keys on
the **column** (migration 0099). An acked row would go invisible to dedup,
so the next sighting either trips `_after_tag_mutation` →
`sync_resolved_at_with_tags` (silently stamping `resolved_at` and
re-paging) or raises `UniqueViolation` inside the nursery pass. A real
column costs a migration + `store/types.py` + three mapper SELECT lists +
baseline regen and buys nothing (no read path needs it indexed).

*Implementer must:* ack via `update_ref(meta_patch=…)`, never via `tag`;
never add `alert-state:acknowledged` to `AlertHandler._LIFECYCLE_TAGS`;
keep `raise_alert` and `resolve_stale_alerts` NOT filtering on the ack;
apply the filter in `list_open_alerts`, the `nav` badge,
`_check_alert_backlog_rot` (else an acked alert >7d reads as response-loop
rot) and the remediation router (else it files a gripe for an acked
condition); show acked rows dimmed with their expiry on `/alerts` rather
than hidden; ensure no producer's `extra_meta` uses the key `acked_until`;
update `precis-alert-help` §Triage, which still teaches resolve-as-dismiss.

Acceptance: acking silences the id in `list_open_alerts`, the `/alerts`
badge, `_check_alert_backlog_rot` and the router **without** setting
`resolved_at` or removing `alert-state:open`; the next producer pass bumps
`seen_count` as usual and does not re-page. A resolve → re-raise does *not*
carry the ack forward — assert this, don't "fix" it.

## 2. `/status` onto the panel (optional)

`precis_web/routes/status.py` still computes its liveness sections itself
(through the same `health_checks` functions, so there is one truth already).
Refactoring it onto `health_digest.health_panel()` would make the web page
and the agent read literally the same object; `/status/backlog` keeps
computing live for a human who clicks it. Confirm by diffing a page render
before/after. Skipped at ship to keep the diff to the agent surface.

## 3. Call-site constants

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
