# artifact_kinds + ref_artifacts — schema-only orphan tables

Both exist in the baseline schema (and `artifact_kinds` is in
`schema_dump.py`'s seed-dump list) but no Python in src/ reads or writes
either — the derived-queue family registry they were minted for became
self-contained pass closures with their own lease surfaces (`chunk_claims`,
`app_state` markers). Decide: drop via a forward migration, or adopt.
Owner `src/precis/store/schema_dump.py` + migrations. Mechanical once
decided; found during the 2026-08 docs consolidation.
