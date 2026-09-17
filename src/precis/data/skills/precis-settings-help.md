---
id: precis-settings-help
title: precis — DB-resident settings (config that isn't a secret)
summary: non-secret fleet config lives in the DB (DB row beats env var beats default); a kind gated on an unset key raises Unsupported naming the key — ask the operator to set it
answers:
  - a kind raised Unsupported because a setting is missing — what do I do?
  - which wins if a setting is in the DB and also set as an env var?
  - can I change a setting from here?
applies-to: any verb raising Unsupported over a missing setting; KindSpec.requires_setting
status: active
tags: workflow, troubleshooting
---

# precis-settings-help — non-secret config that lives in the DB

Settings are the fleet's non-secret knobs — spend caps, polite-pool
contact identities, and anything a kind's registration is gated on. A DB
row always beats the env var, which always beats the compiled default.
There is no verb that reads or writes them.

## A kind raised Unsupported because a setting is missing

```text
Unsupported: kind '<kind>' is registered but disabled in this build
(missing setting: contact.polite_email)
```

The error names the key. Ask the operator to set it on the `/settings` web
page; the kind registers on the next server start. Don't retry the verb.
A kind that is disabled for another reason is covered by
[[precis-kinds-disabled-help]].

## See also

- [[precis-kinds-disabled-help]] — the other reasons a kind is absent here.
- [[precis-status]] — per-process runtime facts (build, DB connection,
  migration state).
