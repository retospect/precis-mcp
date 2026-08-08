# Centralize PRECIS_ env vars

381 unique PRECIS_ strings; PrecisConfig declares 19. Replace ad-hoc
os.environ.get with requires_env/requires_secret, then flip
PrecisConfig.extra to forbid. Owner `src/precis/config.py`,
`src/precis/kind_gate.py`. Mechanical but broad.
