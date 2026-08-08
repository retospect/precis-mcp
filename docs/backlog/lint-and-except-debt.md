# Lint / exception debt

Tighten broad `except Exception` (317 across 141 files; many hide spin
loops). Re-evaluate the ruff ignores RUF012 + B905 (can hide real bugs).
Mechanical, incremental.
