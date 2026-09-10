"""Bundled shipped ``checklist`` definitions (docs/backlog/checklist-kind.md).

One YAML file per checklist, synced into the DB on boot by
:mod:`precis.jobs.checklist_sync` (upsert-by-item-name, content-hash
gated). Slice 1 ships only ``test-fixture-checklist.yaml`` — a minimal
fixture proving the sync mechanism; the real pcb-tapeout content arrives
in slice 2 (curated from the Perplexity survey + the design session,
see ``docs/backlog/checklist-kind.md``).

To edit a shipped item, edit the YAML in place — the next boot's sync
inserts a new rev for anything that changed (append-only; nothing here
is rewritten in the DB, ever).
"""
