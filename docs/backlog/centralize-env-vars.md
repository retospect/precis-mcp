# Centralize PRECIS_ env vars

381 unique PRECIS_ strings; PrecisConfig declares 19. Replace ad-hoc
os.environ.get with requires_env/requires_secret, then flip
PrecisConfig.extra to forbid. Owner `src/precis/config.py`,
`src/precis/kind_gate.py`. Mechanical but broad.

[db-resident-settings](db-resident-settings.md) supplies the destination
layer (DB-backed `precis.settings` + `requires_setting`); this sweep
should route values there per that item's vault-vs-settings-vs-env
boundary, not blanket-promote everything to PrecisConfig.
