# Run the cron → level:recurring backfill against prod

ADR 0061 retired kind='cron' in code; the data half
(`scripts/migrate_cron_to_recurring.py`, dry-run by default) has NOT been run
against prod. A human must review the dry-run first: the old free-form
recurrence vocabulary doesn't map 1:1 (weekly defaults to Monday; some
`every N unit` shapes outside the new grammar stay as cron refs for manual
handling). Run dry-run → review → `--commit`. Ops, high priority.
