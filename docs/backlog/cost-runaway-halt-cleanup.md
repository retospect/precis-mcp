# Ops: drop the manual cost-runaway halt tags

258 todos under 43019 carry `OPEN:halt:orphan-subtree-cost-runaway` and 85
jobs were flipped to STATUS:cancelled by hand (2026-08-06, set_by='user').
With the `_drop_orphaned` dispatch fix deployed the case is covered
structurally — DELETE the manual ref_tags rows. Mechanical prod SQL.
