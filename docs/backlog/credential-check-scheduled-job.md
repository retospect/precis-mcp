---
status: draft
title: Run the weekly credential check as a scheduled job that raises an alert
pillar: platform
---

# Run the weekly credential check as a scheduled job that raises an alert

The weekly check (`precis secret check`, runbook `docs/runbooks/secrets-check.md`,
gr496305) only runs when someone runs `/whatneedsdoing`. A scheduler cadence
(like `cfp_watch`) would run `precis.secret_status.weekly_report` weekly
and raise an alert per `failed` credential, so a revoked key does not wait for
a session. Same security rules as the manual check: fixed `_PROBES` endpoints
only, no value printed, logged or stored (the alert text carries the secret name,
HTTP status and key URL). test: the job's alert text never contains a
configured value.
