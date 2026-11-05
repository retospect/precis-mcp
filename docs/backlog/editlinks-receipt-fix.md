# Memory receipt follow-up (gr564180, td564404)

Correct the new receipt tests to request anchored edits explicitly; keep
the existing dry-run mode contract. Share one receipt collector across
nested batch edits so the outer response reports every source's effects.
Retain chunk identity and render card restoration targets as chunk handles,
which the link parser accepts. Cover batch apply/preview and parse and
execute the printed card restore call. Validate touched Python files with
Ruff only, as required by `scratch/codex-fix.md`; commit without landing.
