# Advisory code-smell pass (DRY / class design) — periodic, not the gate

The gate now enforces the deterministic design checks (import-linter
contracts in `pyproject.toml`, `tests/test_schema_design.py`). The
judgment-laden smell detectors deliberately stayed out — under sibling
gate congestion an opinionated linter reddening `/land` would get
suppressed within a week. Give them a home as a periodic advisory
instead, alongside /whatneedsdoing's hygiene scan or a
`scripts/*-review`-style report:

- `pylint --disable=all --enable=duplicate-code` — the only real DRY
  detector (ruff has none); expect noise from near-duplicate SQL
  builders, so report top clusters, don't threshold.
- pylint design metrics ruff lacks (`too-many-ancestors`,
  `too-few-public-methods`) and/or ruff's `PLR09*` with generous
  thresholds — feeds the improve-split-giant-modules cohort.
- `vulture` dead-code sweep (low confidence hits are FP-prone; min
  confidence 80+).

Findings route to backlog items / gripes, not exit codes. Related:
lint-and-except-debt (ruff-ignore re-evaluation belongs there).

Test: the advisory run completes on today's tree and emits a ranked
report; nothing is wired into scripts/ship.
