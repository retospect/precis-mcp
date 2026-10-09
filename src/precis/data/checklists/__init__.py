"""Bundled shipped ``checklist`` definitions (docs/backlog/checklist-kind.md).

One YAML file per checklist, synced into the DB on boot by
:mod:`precis.jobs.checklist_sync` (upsert-by-item-name, content-hash
gated). ``pcb-tapeout.yaml`` is the first real instance — the pcb
pre-tapeout checklist, kind-default for every ``pcb`` ref
(``default_for: [pcb]``); its three ``decidability: tool`` items are
decided live by the checkers in :mod:`precis.checklist.pcb`, keyed by
item name. ``test-fixture-checklist.yaml`` is the sync mechanism's test
fixture, not a checklist anyone runs.

To edit a shipped item, edit the YAML in place — the next boot's sync
inserts a new rev for anything that changed (append-only; nothing here
is rewritten in the DB, ever). An item removed from the file is retired.
"""
