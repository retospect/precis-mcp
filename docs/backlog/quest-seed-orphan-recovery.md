---
status: draft
pillar: quests
---

# Audit-less bulk soft-delete of job refs

From the gr204309 diagnosis (2026-08-12). The orphaned-seed half shipped:
`src/precis/quest/compute.py` re-mints a fresh seed job under the same todo
through the automatic infra-repair path (windowed budget,
`_SEED_INFRA_RETRY_WINDOW_HOURS`).

Nine job refs of qu164903's autocatpath seed todos were soft-deleted in one
transaction (2026-08-11 14:00:58Z) with ZERO ref_events — something bypassed
`Store.retire_ref`/`append_event` (raw SQL or untracked tooling, actor
unknown). Find the writer (grep tooling/crons for bulk
`UPDATE refs SET retired_at`), and consider a DB trigger or store-layer
invariant so kind='job' soft-deletes always leave an event trail.
