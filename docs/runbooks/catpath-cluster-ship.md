# Shipping catpath / autocatpath code to the cluster

**When.** New catpath code must reach the fleet. There is no PyPI release: the
channel is a wheelhouse seeded by `scripts/deploy` (gr263082). General deploy:
[`cluster-deploy`](./cluster-deploy.md).

1. **Bump catpath's version** (pyproject) — mandatory. Hosts `uv pip install`
   the wheelhouse wheel, and a same-version wheel with new code is a **no-op on
   already-seeded hosts** (the version-reuse trap in
   `docs/backlog/catpath-wheel-version-reuse.md`). Catpath convention: ruff +
   full pytest green first, then commit + push + tag `vX.Y.Z`.
2. **Trap:** running `uv run` in catpath after the bump rewrites catpath's own
   `uv.lock` self-version → dirty checkout → deploy's wheel preflight dies.
   Commit the lock sync too. Lock-only commits past the precis pin are fine —
   the provenance check diffs only `src` + `pyproject.toml`.
3. In precis: bump the `autocatpath>=` floor (2 places: the `catalyst` and
   `catalyst-gpu` extras), set `rev` in `[tool.uv.sources]` to the release
   commit (the source is rev-pinned since 2026-10-07 — catpath main runs
   ahead of its tags, and a `branch = "main"` source would re-lock onto an
   untagged, energy-moving bump), then run `uv lock -P autocatpath` (it
   resolves the **remote** commit — push first). Ship via `/go`.
4. **Deploy needs `PRECIS_CATPATH_DIR=<catpath checkout>`** — the default
   `~/catpath` does not exist on the controller. With the floor bumped,
   `scripts/deploy` preflight builds the new wheel from that checkout
   (provenance-checked against the `uv.lock` pin) and seeds every plugin host's
   `/opt/precis/wheels`.

Related preflight failures (checkout ahead of the lock pin; skipping the wheel
check with `PRECIS_DEPLOY_SKIP_CATPATH_WHEEL=1`) are in
[`cluster-deploy`](./cluster-deploy.md) §Preflight traps.
